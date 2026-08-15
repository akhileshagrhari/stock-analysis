"""Local-model extraction via LM Studio — the cost floor.

DESIGN §11.5 lists a local LLM fallback as worth benchmarking "purely as a cost
floor, given the 2,500-report backfill scenario". This is that benchmark. It is
not a recommendation.

TWO REASONS TO EXPECT WORSE RESULTS THAN THE API PATH
-----------------------------------------------------
**The input is degraded.** Small local models cannot take PDF document blocks,
so the located pages are flattened to text here. Financial statements are
column-aligned tables, and flattening is exactly the operation that destroys
column alignment — which is how a reader tells the current year from the
prior-year comparative. Table detection recovers some of it; it does not recover
all of it.

**The context is smaller.** Sixty pages of statements runs well past what a 7B
model on 16GB can attend to, so the text is truncated to a character budget.
Truncation drops the notes first, and the notes are where contingent liabilities
and the auditor's remarks live.

So the honest expectation is that this fails DESIGN's ">=95% of extractions pass
arithmetic validation" bar. The point of wiring it up is that the validators and
the bake-off harness already exist, so "how much worse" becomes a measurement
rather than an argument. Run it, read the confidence distribution, and decide
whether the gap is worth the API spend.
"""

from __future__ import annotations

import json
import logging
import time

import pymupdf
import requests
from pydantic import ValidationError

from stockanalysis.config import settings
from stockanalysis.extract.claude import ExtractionJob, ExtractionResult, Usage
from stockanalysis.extract.jsonschema import to_api_schema
from stockanalysis.extract.prompts import EXTRACTION_SYSTEM_PROMPT, user_instruction
from stockanalysis.extract.schema import AnnualReportExtraction

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:1234/v1"

# Roughly 3.7 chars/token, so ~28k characters is ~7.5k tokens of statements
# plus a ~1.8k-token system prompt — a fit for the 8k-16k context a 7B model
# runs at on a 16GB machine.
DEFAULT_MAX_CHARS = 28_000

# Appended to the shared system prompt. The instructions themselves stay
# byte-identical to the API path so the bake-off compares models, not prompts.
TEXT_INPUT_NOTE = """

## A note on this input

You are receiving text extracted from the report's pages, not the pages
themselves. Tables have been flattened, with cells separated by the pipe
character. Column alignment is therefore approximate: identify the current-year
column from the dates in the header row rather than from horizontal position,
and if you cannot establish which column is which, return null and say so in
extraction_notes rather than guessing.
"""


# A table rendering must account for at least this fraction of the page's
# non-trivial lines to be trusted in place of the raw text. Calibrated against
# Reliance's FY2026 cash flow statement, where the detector found two tables
# covering 17% of the page.
MIN_TABLE_COVERAGE = 0.6


def _line_coverage(raw: str, rendered: str) -> float:
    """Fraction of the page's substantive lines that survive into `rendered`.

    Short lines are ignored: bare numbers and one-word fragments reappear
    inside table cells for reasons that say nothing about whether the table
    captured the page.
    """
    lines = {ln.strip() for ln in raw.splitlines() if len(ln.strip()) > 12}
    if not lines:
        return 1.0
    return sum(1 for ln in lines if ln in rendered) / len(lines)


def render_page(page: pymupdf.Page) -> str:
    """One page as text, preferring detected tables over raw flow.

    Raw text extraction reads a financial statement row-major and discards the
    column structure, so "Revenue 12,450 11,200" becomes ambiguous about which
    number is this year. Table detection keeps the cells separated; the page
    heading is carried along because it is what distinguishes the consolidated
    balance sheet from the standalone one.

    But a detected table is not necessarily *the* table. On Reliance's FY2026
    cash flow statement the detector returns two fragments holding 17% of the
    page, and returning only those silently deletes the Statement of Cash Flows
    — taking OCF, and with it the CFO/PAT and accruals checks that DESIGN calls
    the most reliable published warning signs, out of every downstream factor.
    A rendering that loses most of the page is evidence the detector misread it,
    so fall back to raw text: approximate column alignment beats absent rows.
    """
    raw = page.get_text("text")

    try:
        found = page.find_tables()
        tables = list(found.tables) if found else []
    except Exception as e:  # noqa: BLE001 - table finder is heuristic, may fail
        log.debug("table detection failed on page %d: %s", page.number, e)
        tables = []

    if not tables:
        return raw

    heading = "\n".join(raw.splitlines()[:4])
    rendered = []
    for table in tables:
        rows = table.extract()
        rendered.append(
            "\n".join(
                " | ".join((cell or "").strip() for cell in row) for row in rows
            )
        )
    out = heading + "\n" + "\n\n".join(rendered)

    coverage = _line_coverage(raw, out)
    if coverage < MIN_TABLE_COVERAGE:
        log.debug(
            "page %d: tables cover %.0f%% of the text, using raw flow instead",
            page.number, coverage * 100,
        )
        return raw
    return out


def pdf_to_text(pdf_bytes: bytes, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, bool]:
    """Flatten the located pages to text. Returns (text, was_truncated).

    Truncation is reported rather than silent: an extraction from a truncated
    document is missing whole sections, and the caller needs to be able to say
    so in the attempt record instead of leaving a reviewer to wonder why the
    contingent liabilities are null.
    """
    parts: list[str] = []
    used = 0
    truncated = False

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            rendered = f"\n--- page {page.number + 1} ---\n{render_page(page)}"
            remaining = max_chars - used
            if len(rendered) > remaining:
                truncated = True
                # Keep a partial page rather than emitting nothing. A single
                # dense statement page can exceed the whole budget on a small
                # context window, and dropping it whole turns "the context is
                # too small" into "the PDF contained no text" — the same
                # symptom a scanned document produces, diagnosed differently.
                if remaining > 200:
                    parts.append(rendered[:remaining])
                break
            parts.append(rendered)
            used += len(rendered)

    return "".join(parts), truncated


class LocalExtractor:
    """Talks to LM Studio's OpenAI-compatible endpoint.

    Same interface as `ClaudeExtractor` — `.model` and `.extract(job)` — so the
    pipeline, the validators and the bake-off need no special-casing.
    """

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_tokens: int = 4096,
        timeout: float | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_chars = max_chars
        self.max_tokens = max_tokens
        self.timeout = timeout or settings.extraction_timeout_seconds

    def _payload(self, job: ExtractionJob, text: str, truncated: bool) -> dict:
        instruction = user_instruction(job.company, job.symbol, job.fiscal_year)
        if truncated:
            instruction += (
                "\n\nNOTE: this text was truncated to fit the context window, so "
                "later sections of the report are absent. Return null for "
                "anything you cannot find and record the truncation in "
                "extraction_notes."
            )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT + TEXT_INPUT_NOTE},
                {"role": "user", "content": f"{instruction}\n\n---\n\n{text}"},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "annual_report_extraction",
                    "strict": True,
                    "schema": to_api_schema(AnnualReportExtraction),
                },
            },
            # Local models accept sampling parameters; zero temperature is the
            # right default for an extraction task.
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }

    def extract(self, job: ExtractionJob) -> ExtractionResult:
        started = time.monotonic()

        def fail(message: str) -> ExtractionResult:
            return ExtractionResult(
                job=job, model=self.model, mode="LOCAL",
                latency_seconds=time.monotonic() - started, error=message,
            )

        try:
            text, truncated = pdf_to_text(job.pdf_bytes, self.max_chars)
        except Exception as e:  # noqa: BLE001 - a corrupt PDF is a review case
            return fail(f"text extraction failed: {type(e).__name__}: {e}")

        if not text.strip():
            return fail("no text could be extracted from the located pages")

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=self._payload(job, text, truncated),
                timeout=self.timeout,
            )
        except requests.ConnectionError:
            return fail(
                f"cannot reach LM Studio at {self.base_url} — start the local "
                f"server (LM Studio > Developer > Start Server, or `lms server start`)"
            )
        except requests.RequestException as e:
            return fail(f"{type(e).__name__}: {e}")

        if resp.status_code != 200:
            return fail(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as e:
            return fail(f"unexpected response shape: {type(e).__name__}: {e}")

        usage_raw = body.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0) or 0,
            output_tokens=usage_raw.get("completion_tokens", 0) or 0,
        )
        latency = time.monotonic() - started

        try:
            payload = AnnualReportExtraction.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as e:
            # Small models routinely miss a strict schema even with constrained
            # decoding. It is a real failure mode and belongs in the bake-off
            # numbers, not swallowed.
            return ExtractionResult(
                job=job, model=self.model, mode="LOCAL", usage=usage,
                latency_seconds=latency,
                error=f"schema validation failed: {type(e).__name__}: {e}",
            )

        if truncated and payload.extraction_notes is None:
            payload.extraction_notes = (
                "Input text was truncated to fit the local model's context window."
            )

        return ExtractionResult(
            job=job, model=self.model, mode="LOCAL", payload=payload,
            usage=usage, latency_seconds=latency,
        )


def list_local_models(base_url: str = DEFAULT_BASE_URL) -> list[str]:
    """Model ids the local server currently has loaded."""
    resp = requests.get(f"{base_url}/models", timeout=10)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]
