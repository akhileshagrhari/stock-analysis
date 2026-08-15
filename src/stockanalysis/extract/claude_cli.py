"""Extraction through the Claude Code CLI — the no-API-key path.

Claude Pro covers the `claude` CLI; it does not cover Developer Platform
credits, which is what the `anthropic` SDK spends. This backend reaches a
frontier model through the CLI's headless mode (`claude -p`) so the pipeline can
be exercised end to end without topping up an API balance.

WHAT IT COSTS YOU, IN ORDER OF IMPORTANCE
-----------------------------------------
**The input is degraded, same as the local path.** The CLI's `Read` tool
rasterises PDFs via poppler, which is both absent on a stock macOS box and
ruinous for sixty pages. So the located pages are flattened to text by the same
`pdf_to_text` the local backend uses, and column alignment degrades the same
way. Unlike the local path there is no truncation — a 60-page report flattens to
roughly 85k tokens, comfortably inside the context window — so the notes, and
with them contingent liabilities and the auditor's remarks, do survive.

**It is the most expensive option per report.** Measured at ~$1.85 for
Reliance's FY2026 report against a working estimate of ~$0.75 through the API
and ~$0.38 batched. Every invocation is a cold cache write at 1.25x, there is no
Batch API and therefore no 50% discount, and flattened statement text tokenises
badly (168k characters came to 95k tokens). It is cheaper only in the sense that
it draws on a subscription you have already paid for.

**There is no batching.** Each report is one process and roughly 35 seconds.
A Nifty 100 x 3yr backfill is 300 serial invocations, which is a different
proposition from one batch submission and is not what subscription rate limits
are sized for. Use this to prove the pipeline and to run the bake-off; use the
API path for a backfill.

Cost accounting does survive: the CLI reports `total_cost_usd` and full token
counts in its JSON envelope, so the bake-off's cost column stays meaningful.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time

from pydantic import ValidationError

from stockanalysis.config import settings
from stockanalysis.extract.claude import (
    ExtractionJob,
    ExtractionResult,
    ExtractorUnavailableError,
    Usage,
)
from stockanalysis.extract.jsonschema import to_api_schema
from stockanalysis.extract.local import TEXT_INPUT_NOTE, pdf_to_text
from stockanalysis.extract.prompts import EXTRACTION_SYSTEM_PROMPT, user_instruction
from stockanalysis.extract.schema import AnnualReportExtraction

log = logging.getLogger(__name__)

CLI_PREFIX = "cli:"
DEFAULT_CLI_MODEL = "claude-opus-5"

# No truncation in practice: 60 located pages flatten to ~340k characters, and
# the point of using a frontier model here is that the notes fit.
DEFAULT_MAX_CHARS = 2_000_000


def _strip_fence(text: str) -> str:
    """Unwrap a ```json fence if the model added one despite instructions."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def _envelope(stdout: str) -> dict:
    """Parse the CLI's JSON envelope.

    The CLI prefixes stdout with advisory lines in some workspaces (an untrusted
    directory, for one), so seek to the first brace rather than parsing from
    position zero.
    """
    start = stdout.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in CLI output: {stdout[:200]!r}")
    return json.loads(stdout[start:])


class ClaudeCLIExtractor:
    """Drives `claude -p`. Same `.model` / `.extract(job)` surface as the others.

    Runs with no tools and a replaced system prompt, which makes each call a
    single-turn completion rather than an agent loop: `num_turns` is 1, nothing
    can wander off and read the filesystem, and the harness's own ~19k-token
    system prompt is not billed on every report.
    """

    def __init__(
        self,
        model: str = DEFAULT_CLI_MODEL,
        binary: str = "claude",
        max_chars: int = DEFAULT_MAX_CHARS,
        timeout: float | None = None,
    ) -> None:
        self.model = f"{CLI_PREFIX}{model}"
        self.api_model = model
        self.binary = binary
        self.max_chars = max_chars
        self.timeout = timeout or settings.extraction_timeout_seconds

        if shutil.which(binary) is None:
            raise ExtractorUnavailableError(
                f"the {binary!r} CLI is not on PATH, so {self.model} cannot be "
                f"reached. Install Claude Code, or use the API path "
                f"(--model claude-opus-5) or a local model (--model local:<id>)."
            )

    def _prompt(self, job: ExtractionJob, text: str) -> str:
        schema = json.dumps(to_api_schema(AnnualReportExtraction), indent=2)
        return (
            f"{user_instruction(job.company, job.symbol, job.fiscal_year)}\n\n"
            f"Return ONLY a JSON object conforming to this schema. No prose, no "
            f"markdown fence.\n\n{schema}\n\n"
            f"---- REPORT TEXT ----\n\n{text}"
        )

    def extract(self, job: ExtractionJob) -> ExtractionResult:
        started = time.monotonic()

        def fail(message: str, usage: Usage | None = None) -> ExtractionResult:
            return ExtractionResult(
                job=job, model=self.model, mode="CLI", usage=usage or Usage(),
                latency_seconds=time.monotonic() - started, error=message,
            )

        try:
            text, truncated = pdf_to_text(job.pdf_bytes, self.max_chars)
        except Exception as e:  # noqa: BLE001 - a corrupt PDF is a review case
            return fail(f"text extraction failed: {type(e).__name__}: {e}")

        if not text.strip():
            return fail("no text could be extracted from the located pages")

        cmd = [
            self.binary, "-p",
            "--output-format", "json",
            "--model", self.api_model,
            "--system-prompt", EXTRACTION_SYSTEM_PROMPT + TEXT_INPUT_NOTE,
            "--allowed-tools", "",
        ]
        # Run outside the project so a CLAUDE.md or settings.json cannot alter
        # the prompt and quietly make the bake-off compare two different things.
        try:
            with tempfile.TemporaryDirectory() as cwd:
                proc = subprocess.run(
                    cmd, input=self._prompt(job, text), cwd=cwd,
                    capture_output=True, text=True, timeout=self.timeout,
                )
        except subprocess.TimeoutExpired:
            return fail(f"CLI timed out after {self.timeout:.0f}s")
        except OSError as e:
            return fail(f"could not run {self.binary!r}: {e}")

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            return fail(f"CLI exited {proc.returncode}: {detail}")

        try:
            env = _envelope(proc.stdout)
        except (ValueError, json.JSONDecodeError) as e:
            return fail(f"unparseable CLI envelope: {type(e).__name__}: {e}")

        raw_usage = env.get("usage") or {}
        usage = Usage(
            input_tokens=raw_usage.get("input_tokens", 0) or 0,
            output_tokens=raw_usage.get("output_tokens", 0) or 0,
            cache_read_tokens=raw_usage.get("cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=raw_usage.get("cache_creation_input_tokens", 0) or 0,
        )
        cost = env.get("total_cost_usd")

        if env.get("is_error"):
            return fail(f"CLI reported an error: {env.get('subtype')}", usage)

        try:
            payload = AnnualReportExtraction.model_validate(
                json.loads(_strip_fence(env.get("result", "")))
            )
        except (json.JSONDecodeError, ValidationError) as e:
            return ExtractionResult(
                job=job, model=self.model, mode="CLI", usage=usage,
                reported_cost_usd=cost,
                latency_seconds=time.monotonic() - started,
                error=f"schema validation failed: {type(e).__name__}: {e}",
            )

        if truncated and payload.extraction_notes is None:
            payload.extraction_notes = (
                "Input text was truncated before it reached the model."
            )

        return ExtractionResult(
            job=job, model=self.model, mode="CLI", payload=payload, usage=usage,
            reported_cost_usd=cost, latency_seconds=time.monotonic() - started,
        )
