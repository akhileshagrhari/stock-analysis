"""LLM narrative generation for signals — DESIGN §6.4.

The score is arithmetic and the prose is generated. That split is the whole
design: Claude is handed the factor breakdown, the flags and the news mix, and
asked to *explain* a rating it is never allowed to revise. Nothing here feeds
back into a number.

Three things about the shape of this module are deliberate.

**The stable half of the prompt is a cached system prompt.** A narrative pass
covers the whole universe, and every call in it shares the same description of
the model, the weights and the flag rules — a hundred re-sends of identical
context. That description sits in the system prompt behind a cache breakpoint,
and only the per-company block varies, so the first call writes the cache and
the rest can read it at roughly a tenth of input price.

That saving is *conditional*, and the condition fails silently. A prefix shorter
than the model's minimum (512 tokens on Opus 5) is simply not cached — no error,
no warning, just `cache_creation_input_tokens: 0` and full price on every call.
The system prompt currently sits around 2.5k characters, comfortably past the
threshold on any plausible tokenisation, and a test pins that length so a future
trim cannot quietly turn caching off. The exact token count is not asserted here
because measuring it needs a live API call.

**The first call is issued alone, then the rest fan out.** A cache entry is
readable only once the response that wrote it has started streaming, so firing
all hundred at once means all hundred miss and pay full price. One warm-up call,
then the pool.

**Failures are per-company, except the ones that are not.** A timeout on one
company costs that company its narrative and nothing else. A bad API key costs
every company its narrative, and discovering that a hundred times over is a
hundred pointless round trips — so authentication and permission errors abort
the pass instead of being swallowed. The old version caught everything and
returned None, which made a missing key indistinguishable from a quiet model.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.factors import redflags
from stockanalysis.factors.composite import (
    BUY_THRESHOLD,
    FAMILY_WEIGHTS,
    SELL_THRESHOLD,
)
from stockanalysis.serve.queries import SentimentCounts, sentiment_counts

log = logging.getLogger(__name__)

# Internal-tag leakage is a documented failure mode when thinking is suppressed.
# Thinking is left on here, so this is belt-and-braces rather than the fix — but
# a stray tag reaching the `signals` table would be visible to every reader of
# the dashboard, and stripping it costs one regex.
_INTERNAL_TAG = re.compile(r"</?(?:thinking|answer|response|output)>", re.IGNORECASE)

FAMILY_ORDER = ["value", "quality", "growth", "momentum", "sentiment"]


class NarrativeUnavailable(RuntimeError):
    """Raised when no call in the pass can succeed — bad credentials, no key.

    Distinct from a single narrative coming back empty. This aborts the pass;
    that one leaves a NULL in one row.
    """


@dataclass(frozen=True)
class NarrativeInput:
    """Everything the model is told about one company. No DB access downstream."""

    isin: str
    nse_symbol: str
    name: str
    as_of: dt.date
    composite_score: float
    signal: str
    sector: str | None = None
    coverage: float | None = None
    red_flags: tuple[str, ...] = ()
    unknown_flags: tuple[str, ...] = ()
    # Family -> 0-100 percentile within the scored universe. A family the
    # company has no data for is absent, never zero: zero reads as "worst in
    # the sector", which is the opposite of "we could not measure it".
    family_scores: dict[str, float] = field(default_factory=dict)
    news: SentimentCounts | None = None


def _system_prompt() -> str:
    """The stable half of every request. Cached; keep it byte-identical per run.

    Built from the live model constants rather than a transcription of them, so
    a weight change in DESIGN cannot leave this prompt quietly describing last
    month's model to the analyst writing the copy.
    """
    weights = "\n".join(
        f"- {family.capitalize()}: {FAMILY_WEIGHTS[family]:.0%} of the composite"
        for family in FAMILY_ORDER
        if family in FAMILY_WEIGHTS
    )
    flags = "\n".join(
        f"- {d.name}: {d.description}" for d in redflags.DEFINITIONS if d.reachable
    )
    return f"""You write one-paragraph explanations of equity signals for an Indian \
(NSE/BSE) factor model. An analyst reads them next to the numbers.

HOW THE SCORE IS PRODUCED
Each company is scored on five factor families. Every factor is z-scored against \
its own sector, sign-adjusted so higher is always better, averaged within its \
family, and combined:

{weights}

The composite is mapped to a 0-100 percentile **within the scored universe on \
that date**. This is the single most important thing to get right in your prose: \
the score is relative, never absolute. 80 means "top of this universe today", \
not "cheap". Never call a company cheap, expensive, high-quality or fast-growing \
in absolute terms — say it ranks well or badly on that family.

Family scores are percentiles on the same 0-100 scale. Above 65 is a strength, \
below 35 is a weakness, and the middle is unremarkable and usually not worth a \
sentence.

SIGNAL THRESHOLDS
- BUY at {BUY_THRESHOLD:.0f} and above
- HOLD between {SELL_THRESHOLD:.0f} and {BUY_THRESHOLD:.0f}
- SELL below {SELL_THRESHOLD:.0f}

RED FLAGS OVERRIDE THE SCORE
A tripped red flag forces SELL regardless of how good the factors are. When a \
flag is present it is the headline, and the factor scores are context for why \
the override matters:

{flags}

A flag listed as unknown means the data to evaluate it was missing — that is not \
a clean bill of health, and it is worth one clause when the signal is BUY.

COVERAGE
Coverage is the share of model weight backed by real data. Below about 60% the \
score rests on a subset of the model; say so plainly.

HOW TO WRITE
- Two or three sentences. No preamble, no header, no bullet points, no markdown.
- Lead with what drives the signal, then the main qualifier.
- The score is fact. Explain it; never argue with it, hedge it, or suggest a \
different rating.
- Name the specific families doing the work. "Strong fundamentals" says nothing \
that the number did not already say.
- Plain professional prose. No hype, no disclaimers, no investment advice, no \
price targets, no restating the numeric score back to the reader.
- Write only the explanation itself."""


def _fmt_family(scores: dict[str, float]) -> str:
    lines = []
    for family in FAMILY_ORDER:
        value = scores.get(family)
        if value is None:
            lines.append(f"- {family.capitalize()}: not measured (no data)")
        else:
            lines.append(f"- {family.capitalize()}: {value:.0f}/100")
    return "\n".join(lines)


def _fmt_news(counts: SentimentCounts | None, window_days: int) -> str:
    if counts is None or counts.total == 0:
        return f"News (last {window_days} days): no scored coverage."
    return (
        f"News (last {window_days} days): {counts.positive} positive, "
        f"{counts.negative} negative, {counts.neutral} neutral "
        f"({counts.total} articles)."
    )


def build_user_prompt(item: NarrativeInput, window_days: int | None = None) -> str:
    """The volatile half — everything that differs per company.

    Kept strictly after the cached system prompt. Anything company-specific that
    drifted upward into the system prompt would invalidate the cache on every
    single call and silently triple the cost of the pass.
    """
    if window_days is None:
        window_days = settings.narrative_news_window_days

    sector = item.sector or "unclassified"
    coverage = (
        f"{item.coverage:.0%}" if item.coverage is not None else "unknown"
    )
    red = ", ".join(item.red_flags) if item.red_flags else "none tripped"
    unknown = (
        ", ".join(item.unknown_flags) if item.unknown_flags else "none"
    )

    return f"""Company: {item.name} ({item.nse_symbol}), {sector} sector
As of: {item.as_of:%Y-%m-%d}

Composite score: {item.composite_score:.1f}/100
Signal: {item.signal}
Coverage: {coverage} of model weight backed by data

Family percentiles:
{_fmt_family(item.family_scores)}

Red flags tripped: {red}
Red flags not evaluable: {unknown}

{_fmt_news(item.news, window_days)}

Explain this {item.signal} rating."""


def _extract_text(message: Any) -> str | None:
    """Join every text block. Thinking blocks are skipped, not indexed past.

    `content[0]` is not reliably the answer — with thinking on, the first block
    is a thinking block, and reading `.text` off it raises.
    """
    parts = [
        block.text
        for block in getattr(message, "content", []) or []
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    if not parts:
        return None
    text = _INTERNAL_TAG.sub("", "\n".join(parts)).strip()
    return text or None


class NarrativeGenerator:
    """Writes signal explanations. One instance per pass; safe to reuse."""

    def __init__(
        self,
        client: Any | None = None,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int | None = None,
        max_workers: int | None = None,
        news_window_days: int | None = None,
    ) -> None:
        self.model = model or settings.narrative_model
        self.effort = effort or settings.narrative_effort
        self.max_tokens = max_tokens or settings.narrative_max_tokens
        self.max_workers = max_workers or settings.narrative_max_workers
        self.news_window_days = (
            news_window_days
            if news_window_days is not None
            else settings.narrative_news_window_days
        )
        self._client = client
        self._system = _system_prompt()

    # ------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """Built on first use so importing this module never needs credentials.

        The SDK resolves its own credentials — env var or an `ant auth login`
        profile — so an unset ANTHROPIC_API_KEY is not by itself an error.
        """
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:   # pragma: no cover - dependency is declared
                raise NarrativeUnavailable(
                    "the `anthropic` package is not installed"
                ) from exc
            try:
                self._client = Anthropic()
            except Exception as exc:
                raise NarrativeUnavailable(
                    f"could not construct the Anthropic client: {exc}"
                ) from exc
        return self._client

    def generate(self, item: NarrativeInput) -> str | None:
        """One narrative. None if the model declined or the call failed.

        Raises NarrativeUnavailable for failures that would repeat for every
        other company in the pass.
        """
        try:
            import anthropic
        except ImportError:   # pragma: no cover - dependency is declared
            anthropic = None

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                output_config={"effort": self.effort},
                system=[
                    {
                        "type": "text",
                        "text": self._system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": build_user_prompt(item, self.news_window_days),
                    }
                ],
            )
        except NarrativeUnavailable:
            raise
        except Exception as exc:
            if anthropic is not None and isinstance(
                exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
            ):
                raise NarrativeUnavailable(f"Anthropic API rejected the key: {exc}") from exc
            log.warning("narrative failed for %s: %s", item.nse_symbol, exc)
            return None

        # A safety decline is a successful HTTP call with no usable content.
        # Checking it before reading `content` is what keeps this from raising
        # on an empty block list.
        if getattr(message, "stop_reason", None) == "refusal":
            log.warning("narrative refused for %s", item.nse_symbol)
            return None

        text = _extract_text(message)
        if text is None:
            log.warning("narrative empty for %s", item.nse_symbol)
        return text

    def generate_many(self, items: list[NarrativeInput]) -> dict[str, str | None]:
        """Narratives keyed by ISIN. Every input appears in the result.

        The first call runs alone so it can write the prompt cache; the rest
        read it concurrently. With one input this is just `generate`.
        """
        if not items:
            return {}

        results: dict[str, str | None] = {items[0].isin: self.generate(items[0])}
        rest = items[1:]
        if not rest:
            return results

        workers = max(1, min(self.max_workers, len(rest)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            written = pool.map(self.generate, rest)
            for item, narrative in zip(rest, written, strict=True):
                results[item.isin] = narrative
        return results


# ----------------------------------------------------------------------


def build_inputs(
    db: Database,
    as_of: dt.date,
    rows: list[dict[str, Any]],
    news_window_days: int | None = None,
) -> list[NarrativeInput]:
    """Attach the news mix to already-computed signal rows, in one query.

    `rows` carries what the scoring pass already knows — score, signal, flags,
    family percentiles. The only thing this needs the database for is sentiment,
    and it fetches all of it at once.
    """
    if news_window_days is None:
        news_window_days = settings.narrative_news_window_days

    isins = [str(row["isin"]) for row in rows]
    try:
        counts = sentiment_counts(db, isins, as_of, news_window_days)
    except Exception as exc:
        # Missing news is a thinner narrative, not a failed run.
        log.warning("could not load news for narratives: %s", exc)
        counts = {}

    return [
        NarrativeInput(
            isin=str(row["isin"]),
            nse_symbol=str(row.get("nse_symbol") or row["isin"]),
            name=str(row.get("name") or row.get("nse_symbol") or row["isin"]),
            as_of=as_of,
            composite_score=float(row["composite_score"]),
            signal=str(row["signal"]),
            sector=row.get("sector"),
            coverage=row.get("coverage"),
            red_flags=tuple(row.get("red_flags") or ()),
            unknown_flags=tuple(row.get("unknown_flags") or ()),
            family_scores=dict(row.get("family_scores") or {}),
            news=counts.get(str(row["isin"])),
        )
        for row in rows
    ]
