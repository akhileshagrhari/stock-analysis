"""Claude-backed extraction: one report at a time, or a few hundred via batch.

Two paths, same prompt and same schema:

  `extract()`       synchronous, `messages.parse()`, used interactively and by
                    the bake-off where you want the answer now.
  `submit_batch()`  the Batch API at 50% off, used for the backfill. Latency is
                    irrelevant when filling three years of history, and the
                    discount roughly halves the only real cash cost in the
                    project.

The batch path cannot use `parse()` — batches take a raw request body — so it
carries the JSON schema built by `jsonschema.to_api_schema` and validates the
response itself. Both paths therefore go through the same Pydantic model, which
is what keeps them from drifting apart.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from stockanalysis.config import settings
from stockanalysis.extract.jsonschema import to_api_schema
from stockanalysis.extract.prompts import EXTRACTION_SYSTEM_PROMPT, user_instruction
from stockanalysis.extract.schema import AnnualReportExtraction

log = logging.getLogger(__name__)

# USD per million tokens. Cache writes cost 1.25x input, cache reads 0.1x, and
# the Batch API halves everything.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Sonnet 5 introductory pricing. Encoded rather than hardcoded into the table so
# a bake-off run after the cutover does not silently keep reporting the old cost
# and make Sonnet look cheaper than it is.
_SONNET_INTRO = ("claude-sonnet-5", dt.date(2026, 8, 31), (2.00, 10.00))

_CUSTOM_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _rates(model: str, on: dt.date | None = None) -> tuple[float, float]:
    on = on or dt.date.today()
    name, until, intro = _SONNET_INTRO
    if model == name and on <= until:
        return intro
    if model not in _PRICING:
        raise ValueError(f"no pricing for model {model!r}; add it to _PRICING")
    return _PRICING[model]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @classmethod
    def from_api(cls, usage: Any) -> Usage:
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    def cost_usd(self, model: str, batch: bool = False, on: dt.date | None = None) -> float:
        """Estimated spend. NaN for a model with no published rates.

        Deliberately not raising: cost accounting is bookkeeping, and an
        extraction that succeeded must not be lost because we cannot price it.
        A NaN shows up as a blank in the bake-off table, which is the honest
        rendering of "we don't know".
        """
        try:
            in_rate, out_rate = _rates(model, on)
        except ValueError:
            log.warning("no pricing for model %r; cost not tracked", model)
            return float("nan")
        total = (
            self.input_tokens * in_rate
            + self.cache_creation_tokens * in_rate * 1.25
            + self.cache_read_tokens * in_rate * 0.10
            + self.output_tokens * out_rate
        ) / 1_000_000
        return total * (0.5 if batch else 1.0)


@dataclass
class ExtractionJob:
    """One report to extract. `pdf_bytes` is the locator's output, not the original."""

    filing_id: str
    isin: str
    symbol: str
    company: str
    fiscal_year: int
    pdf_bytes: bytes
    pages_sent: int = 0
    source_pages: str = ""

    @property
    def custom_id(self) -> str:
        """Batch results come back keyed by this, in arbitrary order."""
        return _CUSTOM_ID_SAFE.sub("-", self.filing_id)[:64]


@dataclass
class ExtractionResult:
    job: ExtractionJob
    model: str
    mode: str  # SYNC | BATCH | LOCAL | CLI
    payload: AnnualReportExtraction | None = None
    usage: Usage = field(default_factory=Usage)
    latency_seconds: float = 0.0
    error: str | None = None
    # Spend as reported by the backend itself. The CLI prices its own call and
    # knows about surcharges and tier effects that `_PRICING` does not, so its
    # figure is preferred over one we recompute from token counts.
    reported_cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.payload is not None and self.error is None

    def cost_usd(self) -> float:
        if self.reported_cost_usd is not None:
            return self.reported_cost_usd
        # A local model costs nothing per call. Reporting zero rather than NaN
        # keeps the bake-off's cost column summable across mixed runs — the
        # tradeoff against an API model is the whole point of running one.
        if self.mode == "LOCAL":
            return 0.0
        return self.usage.cost_usd(self.model, batch=self.mode == "BATCH")


def _system_blocks() -> list[dict]:
    """System prompt with the cache breakpoint at its end.

    Render order is tools -> system -> messages, so a breakpoint here caches
    everything up to and including the prompt. The PDF and the company-specific
    instruction sit after it and change every request, which is exactly where
    volatile content belongs.
    """
    return [
        {
            "type": "text",
            "text": EXTRACTION_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _user_content(job: ExtractionJob) -> list[dict]:
    # Document block before the text block: the instruction reads as being about
    # the document that precedes it.
    return [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(job.pdf_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": user_instruction(job.company, job.symbol, job.fiscal_year)},
    ]


def _parse_json_payload(text: str) -> AnnualReportExtraction:
    return AnnualReportExtraction.model_validate(json.loads(text))


class ExtractorUnavailableError(RuntimeError):
    """Raised when a backend cannot be reached before any work is attempted.

    Missing API credentials and an empty LM Studio are ordinary first-run
    states, not failures of the pipeline. Naming them lets the CLI say what to
    do about it rather than surfacing a traceback from inside a vendor SDK.
    """


class ClaudeExtractor:
    def __init__(
        self,
        model: str | None = None,
        client: anthropic.Anthropic | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.model = model or settings.extraction_model
        self.max_tokens = max_tokens or settings.extraction_max_tokens
        self.timeout = timeout or settings.extraction_timeout_seconds
        # Zero-arg construction on purpose: the SDK resolves ANTHROPIC_API_KEY or
        # an `ant auth login` profile, and re-implementing that here would only
        # add a place for a stale key to win.
        if client is None:
            client = anthropic.Anthropic()
            # Construction succeeds with no credentials at all; the SDK does not
            # complain until it builds a request, and then it raises TypeError
            # from several frames down. Check here instead, so an absent key
            # costs nothing rather than surfacing after the locator has spent a
            # minute narrowing a 300-page report. Only when we built the client:
            # an injected one is the caller's business, including the Bedrock and
            # Vertex variants that authenticate by other means entirely.
            if client.api_key is None and client.auth_token is None:
                raise ExtractorUnavailableError(
                    f"no Anthropic credentials found, so {self.model} cannot be "
                    f"reached. Set ANTHROPIC_API_KEY in .env or the environment. "
                    f"Note Claude Pro does not cover this — the SDK spends "
                    f"Developer Platform credits, topped up at "
                    f"console.anthropic.com. To extract for free instead, run a "
                    f"local model: --model local:<model-id>"
                )
        self.client = client

    # ------------------------------------------------------------------
    # Synchronous
    # ------------------------------------------------------------------

    def extract(self, job: ExtractionJob) -> ExtractionResult:
        """Extract one report. Never raises — failures come back as a result
        with `error` set, so a 300-report run does not die on filing 41."""
        started = time.monotonic()
        try:
            resp = self.client.with_options(timeout=self.timeout).messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_system_blocks(),
                messages=[{"role": "user", "content": _user_content(job)}],
                output_format=AnnualReportExtraction,
                # Adaptive thinking, effort left at its default of high. This is
                # a precision task on dense tabular input; the fixed-budget
                # `budget_tokens` form is rejected by these models anyway.
                thinking={"type": "adaptive"},
            )
        except anthropic.APIError as e:
            return ExtractionResult(
                job=job,
                model=self.model,
                mode="SYNC",
                latency_seconds=time.monotonic() - started,
                error=f"{type(e).__name__}: {e}",
            )

        latency = time.monotonic() - started
        usage = Usage.from_api(resp.usage)

        # Check stop_reason before touching content: a refusal returns HTTP 200
        # with an empty or partial content list, so indexing straight into it
        # would raise something unhelpful and lose the reason.
        if resp.stop_reason == "refusal":
            return ExtractionResult(
                job=job, model=self.model, mode="SYNC", usage=usage,
                latency_seconds=latency, error="refusal: request declined by safety classifiers",
            )
        if resp.stop_reason == "max_tokens":
            return ExtractionResult(
                job=job, model=self.model, mode="SYNC", usage=usage,
                latency_seconds=latency,
                error=f"max_tokens: output truncated at {self.max_tokens}; raise it",
            )

        payload = getattr(resp, "parsed_output", None)
        if payload is None:
            return ExtractionResult(
                job=job, model=self.model, mode="SYNC", usage=usage,
                latency_seconds=latency, error="no parsed output in response",
            )

        return ExtractionResult(
            job=job, model=self.model, mode="SYNC", payload=payload,
            usage=usage, latency_seconds=latency,
        )

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    def submit_batch(self, jobs: list[ExtractionJob]) -> str:
        """Submit jobs as one batch; returns the batch id to poll."""
        if not jobs:
            raise ValueError("no jobs to submit")

        seen: set[str] = set()
        for j in jobs:
            if j.custom_id in seen:
                raise ValueError(
                    f"duplicate custom_id {j.custom_id!r} — results are keyed by "
                    f"it, so a collision silently loses an extraction"
                )
            seen.add(j.custom_id)

        schema = to_api_schema(AnnualReportExtraction)
        requests = [
            {
                "custom_id": j.custom_id,
                "params": {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": _system_blocks(),
                    "messages": [{"role": "user", "content": _user_content(j)}],
                    "output_config": {"format": {"type": "json_schema", "schema": schema}},
                    "thinking": {"type": "adaptive"},
                },
            }
            for j in jobs
        ]

        batch = self.client.messages.batches.create(requests=requests)
        log.info("submitted batch %s with %d requests", batch.id, len(jobs))
        return batch.id

    def batch_status(self, batch_id: str) -> tuple[str, dict]:
        batch = self.client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        return batch.processing_status, {
            "processing": counts.processing,
            "succeeded": counts.succeeded,
            "errored": counts.errored,
            "canceled": counts.canceled,
            "expired": counts.expired,
        }

    def collect_batch(
        self, batch_id: str, jobs: list[ExtractionJob]
    ) -> list[ExtractionResult]:
        """Fetch batch results and pair them back to their jobs.

        Results arrive in arbitrary order, so they are keyed by `custom_id` —
        never by position. Pairing by index appears to work on a small test
        batch and then silently attributes company A's financials to company B.
        """
        by_id = {j.custom_id: j for j in jobs}
        results: list[ExtractionResult] = []

        for item in self.client.messages.batches.results(batch_id):
            job = by_id.get(item.custom_id)
            if job is None:
                log.warning("batch %s returned unknown custom_id %s", batch_id, item.custom_id)
                continue

            kind = item.result.type
            if kind != "succeeded":
                detail = getattr(getattr(item.result, "error", None), "type", kind)
                results.append(
                    ExtractionResult(
                        job=job, model=self.model, mode="BATCH", error=f"{kind}: {detail}"
                    )
                )
                continue

            message = item.result.message
            usage = Usage.from_api(message.usage)

            if message.stop_reason == "refusal":
                results.append(
                    ExtractionResult(
                        job=job, model=self.model, mode="BATCH", usage=usage,
                        error="refusal: request declined by safety classifiers",
                    )
                )
                continue

            # With thinking on, the first content block is a thinking block, so
            # content[0].text is not the JSON.
            text = next((b.text for b in message.content if b.type == "text"), None)
            if text is None:
                results.append(
                    ExtractionResult(
                        job=job, model=self.model, mode="BATCH", usage=usage,
                        error=f"no text block in response (stop_reason={message.stop_reason})",
                    )
                )
                continue

            try:
                payload = _parse_json_payload(text)
            except (json.JSONDecodeError, ValidationError) as e:
                results.append(
                    ExtractionResult(
                        job=job, model=self.model, mode="BATCH", usage=usage,
                        error=f"schema validation failed: {e}",
                    )
                )
                continue

            results.append(
                ExtractionResult(
                    job=job, model=self.model, mode="BATCH", payload=payload, usage=usage
                )
            )

        missing = set(by_id) - {r.job.custom_id for r in results}
        for cid in sorted(missing):
            results.append(
                ExtractionResult(
                    job=by_id[cid], model=self.model, mode="BATCH",
                    error="no result returned for this custom_id",
                )
            )

        return results
