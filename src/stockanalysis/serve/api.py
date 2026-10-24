"""FastAPI surface over stored signals — DESIGN §9 phase 4.

Read-only by construction: the database is opened `read_only=True`, so no route
can write even by accident, and several processes can serve the same file at
once.

The database arrives through a FastAPI dependency rather than a module-level
global. That is what makes the endpoints testable — a test overrides `get_db`
and the whole app runs against a temporary database — and it is also where the
three ways a database can be unusable (absent, locked by an ingest, older than
the current schema) turn into a 503 with an explanation instead of a traceback.

Response models use `Optional[float]` throughout and the read layer converts NaN
to None on the way out. A NaN reaching this layer would serialise to a bare
`NaN`, which is not valid JSON and which strict clients reject outright.

With `SA_SERVE_BACKEND=postgres` the same routes read the Postgres copy that
`stockanalysis publish` maintains instead of the DuckDB file — see `db.publish`.

**What the public site calls is `/v1/...`.** Every handler is registered at
two addresses by `route`: under `/v1`, which is the documented contract a
generated client is built from (`stockanalysis export-openapi`), and at the
root, undocumented, so nothing that already calls `/signals` breaks. One
handler behind both means the two cannot drift; a breaking change gets a `/v2`
beside them instead of an edit to either.

Two things sit in front of the routes for a site that is visited, not just
queried. Successful responses carry `Cache-Control: public, max-age=…`
(`SA_API_CACHE_SECONDS`): the data changes once a day, when a run publishes, so
a CDN or browser can answer repeat views for minutes without asking. Errors are
always `no-store` — a 503 during a publish must not be replayed after it. And
browsers on other origins are refused unless named in `SA_API_CORS_ORIGINS`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from stockanalysis.config import settings
from stockanalysis.db.database import (
    Database,
    DatabaseLockedError,
    SchemaOutOfDateError,
)
from stockanalysis.factors import redflags
from stockanalysis.factors.composite import (
    BUY_THRESHOLD,
    FAMILY_WEIGHTS,
    SELL_THRESHOLD,
    ScoringConfig,
    default_factors,
)
from stockanalysis.serve import explain, queries

app = FastAPI(
    title="StockAnalysis API",
    description=(
        "Factor-based equity research for Indian markets. Scores are "
        "percentiles within the scored universe on each date, not absolute "
        "valuations."
    ),
    version="0.3.0",
)



def route(path: str, **kwargs):
    """`app.get`, registering the handler at `/v1{path}` and, undocumented, at `{path}`.

    Registered on the app directly rather than through `include_router`.
    FastAPI now includes a router lazily, as one opaque entry in `app.routes`,
    and the tests that ask Starlette which route wins a path, and that every
    route has a parity check against Postgres, read `app.routes` — through a
    router they would find nothing to check and pass anyway.
    """

    def register(handler):
        app.get(f"/v1{path}", **kwargs)(handler)
        app.get(path, include_in_schema=False, **kwargs)(handler)
        return handler

    return register


SIGNALS = ("BUY", "HOLD", "SELL")
LATEST_PER_INSTRUMENT = "per_instrument"

# What a price chart opens on when the caller names no start date.
DEFAULT_PRICE_WINDOW = dt.timedelta(days=365)


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------


def get_db() -> Iterator[Database]:
    """One read-only connection per request; overridden wholesale in tests.

    DuckDB unless `SA_SERVE_BACKEND=postgres`, in which case the same `queries`
    SQL runs against the copy `stockanalysis publish` maintains. The handle is
    then a `PostgresReader` — not a `Database`, but the same `query` method the
    read layer calls, which is all any route touches.
    """
    if settings.serve_backend == "postgres":
        yield from _postgres_db()
        return
    try:
        db = Database(settings.db_path, read_only=True)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"No database at {settings.db_path}. Run `stockanalysis init` first.",
        ) from exc
    except SchemaOutOfDateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DatabaseLockedError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database is busy — an ingest is probably running. {exc}",
        ) from exc
    try:
        yield db
    finally:
        db.close()


def _postgres_db() -> Iterator[Database]:
    """A pooled Postgres connection, with "cannot serve" turned into a 503.

    Imported lazily so the default DuckDB deployment never needs psycopg.
    """
    from stockanalysis.db.postgres import (
        PostgresUnavailable,
        ServingUnavailable,
        pooled_reader,
    )

    try:
        with pooled_reader() as reader:
            yield reader
    except (PostgresUnavailable, ServingUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


DB = Annotated[Database, Depends(get_db)]


def parse_date(value: str | None, name: str) -> dt.date | None:
    """A malformed date is the caller's mistake — 422 naming the parameter, not a 500."""
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{name} must be YYYY-MM-DD, got {value!r}"
        ) from exc


def parse_as_of(as_of: str | None) -> dt.date | None:
    return parse_date(as_of, "as_of")


def require_instrument(db: Database, isin: str) -> queries.Instrument:
    row = queries.get_instrument(db, isin)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No instrument with ISIN {isin!r}")
    return row


def validate_signal(signal: str | None) -> str | None:
    if signal is None:
        return None
    upper = signal.upper()
    if upper not in SIGNALS:
        raise HTTPException(
            status_code=422,
            detail=f"signal must be one of {', '.join(SIGNALS)}, got {signal!r}",
        )
    return upper


# ----------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------


FactorUnit = Literal["percent", "pp", "multiple", "ratio_vs_trend", "score", "ratio"]
_UNIT_DESCRIPTION = (
    "How to read raw_value. percent: a fraction to show as % (0.0785 is 7.85%). "
    "pp: a difference of two fractions, in percentage points (0.012 is +1.2 pp). "
    "multiple: a ratio to show as × (0.68 is 0.68×). ratio_vs_trend: price over "
    "its 200-day average (0.89 is 11% below trend). score: signed, −1 to +1. "
    "ratio: a plain number with no unit. Null for a factor the current model "
    "does not define."
)


class InstrumentOut(BaseModel):
    isin: str
    nse_symbol: str
    name: str
    sector: str | None = None
    bse_code: str | None = None
    listing_date: dt.date | None = None

    @classmethod
    def of(cls, row: queries.Instrument) -> InstrumentOut:
        return cls(**row.as_dict())


class FactorOut(BaseModel):
    factor_name: str
    raw_value: float | None = None
    sector_zscore: float | None = None
    unit: FactorUnit | None = Field(default=None, description=_UNIT_DESCRIPTION)

    @classmethod
    def of(cls, row: queries.FactorScore) -> FactorOut:
        return cls(
            factor_name=row.factor_name,
            raw_value=row.raw_value,
            sector_zscore=row.sector_zscore,
            unit=explain.factor_unit(row.factor_name),
        )


class PreviousSignalOut(BaseModel):
    as_of: dt.date
    composite_score: float | None = None
    signal: str | None = None
    model_version: str | None = None


class SignalOut(BaseModel):
    isin: str
    nse_symbol: str
    name: str
    sector: str | None = None
    as_of: dt.date
    # Null when coverage fell below the model's floor. An unscored company is
    # not a HOLD, and the API says so rather than inventing a number.
    composite_score: float | None = None
    signal: str | None = None
    coverage: float | None = Field(default=None, ge=0, le=1)
    red_flags: list[str] = Field(default_factory=list)
    unknown_flags: list[str] = Field(default_factory=list)
    narrative: str | None = None
    model_version: str | None = None
    previous: PreviousSignalOut | None = Field(
        default=None,
        description="The same instrument's most recent signal strictly before "
        "this row's as_of, whatever the rows a query returns; null for its first. "
        "A different model_version means the change is partly the model's.",
    )

    @classmethod
    def of(cls, row: queries.Signal) -> SignalOut:
        return cls(
            isin=row.isin,
            nse_symbol=row.nse_symbol,
            name=row.name,
            sector=row.sector,
            as_of=row.as_of,
            composite_score=row.composite_score,
            signal=row.signal,
            coverage=row.coverage,
            red_flags=row.red_flags,
            unknown_flags=row.unknown_flags,
            narrative=row.narrative,
            model_version=row.model_version,
            previous=(
                None if row.previous is None
                else PreviousSignalOut(
                    as_of=row.previous.as_of,
                    composite_score=row.previous.composite_score,
                    signal=row.previous.signal,
                    model_version=row.previous.model_version,
                )
            ),
        )


class SignalDetailOut(SignalOut):
    factors: list[FactorOut] = Field(default_factory=list)

    @classmethod
    def detailed(
        cls, row: queries.Signal, factors: list[queries.FactorScore]
    ) -> SignalDetailOut:
        return cls(
            **SignalOut.of(row).model_dump(),
            factors=[FactorOut.of(f) for f in factors],
        )


class NewsOut(BaseModel):
    published_at: dt.date | None = None
    headline: str | None = None
    label: str | None = None
    score: float | None = None
    source: str | None = None
    url: str | None = None
    relevance: Literal["primary", "mention"] | None = Field(
        default=None,
        description="primary: the company is named in the headline of an article "
        "about it. mention: named only in the body, or one of more than three "
        "companies a roundup lists. Null when the match was not recorded.",
    )
    sentiment_status: Literal["scored", "pending", "not_scored"] = Field(
        default="not_scored",
        description="scored: label and score are set. pending: the company match "
        "clears the attribution threshold and the next scoring run will score "
        "it. not_scored: the match is below the threshold (an uncertain match or "
        "a roundup), so it is never scored. A failed scoring run leaves its "
        "articles pending; the pipeline records no per-article failure.",
    )
    counts_toward_sentiment: bool = Field(
        default=False,
        description="Whether the 30-day news factor reads this article: scored, "
        "and its company match clears the attribution threshold. False for a "
        "roundup scored before it was demoted, though its label is shown.",
    )


class HealthOut(BaseModel):
    status: str
    latest_as_of: dt.date | None = None
    instruments: int = 0
    signals: int = 0
    latest_complete_as_of: dt.date | None = Field(
        default=None,
        description="Newest scoring date on which at least "
        f"{queries.COMPLETE_RUN_SHARE:.0%} of the instruments active that day "
        "got a signal. latest_as_of can be a partial run (16 Aug 2026 scored 4 "
        "of 100); this is the newest date a full list can be read from. Null "
        "when no date qualifies.",
    )


class ScoredDateCountOut(BaseModel):
    as_of: dt.date
    scored: int = Field(description="Instruments with a signal that day.")
    instruments: int = Field(
        description="Instruments listed and not delisted that day; one with no "
        "listing date counts as listed."
    )


class FamilyInfoOut(BaseModel):
    family: str
    meaning: str | None = None
    weight: float = Field(description="Share of the composite, as a fraction (0-1).")


class FactorInfoOut(BaseModel):
    name: str = Field(description="The factor_name stored rows carry.")
    label: str
    meaning: str = Field(
        description="What a positive (sign-adjusted) z-score says about the company."
    )
    family: str
    unit: FactorUnit = Field(description=_UNIT_DESCRIPTION)
    higher_is_better: bool = Field(
        description="Direction of the raw metric before the model's sign flip. "
        "False for debt/equity and accruals, whose z-scores are negated so that "
        "a positive z is always good."
    )


class RedFlagInfoOut(BaseModel):
    name: str
    description: str
    reachable: bool = Field(
        description="False when no data source the system ingests can evaluate "
        "the flag, so it is unknown for every company — never clear."
    )


class ModelInfoOut(BaseModel):
    family_weights: dict[str, float]
    buy_threshold: float
    sell_threshold: float
    scored_dates: list[dt.date]
    version: str = Field(
        description="The current model_version. Stored signals carry their own; "
        "one that differs was scored under another configuration."
    )
    coverage_floor: float = Field(
        description="Fraction (0-1) of the model's weight that must be backed "
        "by data before a company is scored."
    )
    families: list[FamilyInfoOut] = Field(default_factory=list)
    factors: list[FactorInfoOut] = Field(default_factory=list)
    red_flags: list[RedFlagInfoOut] = Field(
        default_factory=list,
        description="Every flag the model checks. A flag is clear only when it "
        "is in neither a signal's red_flags nor its unknown_flags.",
    )
    scored_date_counts: list[ScoredDateCountOut] = Field(
        default_factory=list,
        description="How many companies each date in scored_dates covered, "
        "newest first, so a partial run can be told from a full one.",
    )


class LineItemOut(BaseModel):
    """One row of the annual statement, in the order a statement is read."""

    key: str
    label: str
    per_share: bool = False   # rupees per share; every other line is ₹ crore


# Statement order and units are decided once, in `queries`; a client renders
# `AnnualOut.values` by walking this list rather than inventing its own order.
LINE_ITEMS = [
    LineItemOut(key=key, label=label, per_share=key in queries.PER_SHARE_ITEMS)
    for key, label in queries.ANNUAL_LINE_ITEMS
]


_EPS_ADJUSTED = (
    "₹ per share: eps restated onto today's share basis — the basis of the "
    "stored price series — across every split and bonus with an ex-date after "
    "the filing (see /corporate-actions). Equal to eps when there was none; "
    "comparable across years where eps is not. Null when eps is."
)


_QUARTER_LINE = (
    "₹ crore, as the quarter's XBRL tags it. Null where the filing does not "
    "tag it, and on rows from NSE's results comparison, which carries only "
    "revenue, PAT and EPS."
)


class AnnualOut(BaseModel):
    fiscal_year: int
    basis: str
    period_end: dt.date | None = None
    filing_date: dt.date | None = None
    source: str | None = None
    auditor_opinion: str | None = None
    confidence: float | None = None
    # Keyed by `LineItemOut.key`; null is a figure the filing does not carry,
    # which is not the same thing as a zero.
    values: dict[str, float | None] = Field(default_factory=dict)
    eps_adjusted: float | None = Field(default=None, description=_EPS_ADJUSTED)
    source_url: str | None = Field(
        default=None,
        description="The document the figures were read from: the results "
        "filing's XBRL instance on nsearchives.nseindia.com (source XBRL), or "
        "the annual report PDF (source LLM). Null for rows stored before links "
        "were recorded.",
    )


class QuarterOut(BaseModel):
    period_end: dt.date
    filing_date: dt.date | None = None
    relating_to: str | None = None
    is_consolidated: bool | None = None
    revenue: float | None = None
    pat: float | None = None
    eps: float | None = None
    eps_adjusted: float | None = Field(default=None, description=_EPS_ADJUSTED)
    basis: Literal["CONSOLIDATED", "STANDALONE"] | None = Field(
        default=None, description="From is_consolidated; null when not recorded."
    )
    total_income: float | None = Field(default=None, description=_QUARTER_LINE)
    other_income: float | None = Field(default=None, description=_QUARTER_LINE)
    total_expenses: float | None = Field(default=None, description=_QUARTER_LINE)
    depreciation: float | None = Field(default=None, description=_QUARTER_LINE)
    interest_expense: float | None = Field(
        default=None,
        description=_QUARTER_LINE + " Finance costs; for a bank, the interest it "
        "pays, which is its main operating cost.",
    )
    profit_before_tax: float | None = Field(default=None, description=_QUARTER_LINE)
    tax_expense: float | None = Field(default=None, description=_QUARTER_LINE)
    ebitda: float | None = Field(
        default=None,
        description="Derived, ₹ crore: profit_before_tax + interest_expense + "
        "depreciation, the value factor's own formula. Null for banks and "
        "insurers, and where PBT or depreciation is missing.",
    )
    source_url: str | None = Field(
        default=None,
        description="The results filing's XBRL instance the figures were read "
        "from, on nsearchives.nseindia.com. Null for rows from NSE's results "
        "comparison.",
    )


class FinancialsOut(BaseModel):
    line_items: list[LineItemOut]
    annual: list[AnnualOut]
    quarterly: list[QuarterOut]


class FamilyOut(BaseModel):
    family: str
    meaning: str | None = None
    weight: float
    percentile: float | None = None
    z: float | None = None
    contribution: float | None = None
    factors_measured: int
    factors_total: int
    verdict: str

    @classmethod
    def of(cls, row: explain.FamilyContribution) -> FamilyOut:
        return cls(
            family=row.family,
            meaning=explain.FAMILY_MEANING.get(row.family),
            weight=row.weight,
            percentile=row.percentile,
            z=row.z,
            contribution=row.contribution,
            factors_measured=row.factors_measured,
            factors_total=row.factors_total,
            verdict=row.verdict,
        )


class FamilyPointOut(BaseModel):
    family: str
    percentile: float | None = Field(
        default=None,
        description="0-100 within the universe scored that day; null when no "
        "factor in the family was measured.",
    )
    contribution: float | None = Field(
        default=None,
        description="Signed share of that day's composite z (negative pulled "
        "the score down); the families' contributions sum to it.",
    )


class HistoryPointOut(SignalOut):
    families: list[FamilyPointOut] = Field(
        default_factory=list,
        description="The day's family breakdown, replayed from that day's stored "
        "z-scores with the current model's weights — a row whose model_version "
        "differs is re-derived, as /explanation's stale flag says. Empty when "
        "the day stored no factor rows.",
    )


class DriverOut(BaseModel):
    name: str
    label: str
    meaning: str
    family: str
    raw_value: float | None = None
    # Sign-adjusted: positive is good for the company whichever way the
    # underlying metric runs. See `serve.explain`.
    z: float | None = None
    unit: FactorUnit | None = Field(default=None, description=_UNIT_DESCRIPTION)

    @classmethod
    def of(cls, row: explain.FactorDriver) -> DriverOut:
        return cls(
            name=row.name,
            label=row.label,
            meaning=row.meaning,
            family=row.family,
            raw_value=row.raw_value,
            z=row.z,
            unit=row.unit,
        )


class NewsMixOut(BaseModel):
    positive: int
    negative: int
    neutral: int
    total: int


class RedFlagOut(BaseModel):
    name: str
    description: str | None = None


_FLAG_DESCRIPTIONS = {d.name: d.description for d in redflags.DEFINITIONS}


class ExplanationOut(BaseModel):
    isin: str
    nse_symbol: str
    name: str
    as_of: dt.date
    signal: str | None = None
    composite_score: float | None = None
    coverage: float | None = None
    headline: str
    reasons: list[str] = Field(default_factory=list)
    families: list[FamilyOut] = Field(default_factory=list)
    strengths: list[DriverOut] = Field(default_factory=list)
    weaknesses: list[DriverOut] = Field(default_factory=list)
    # Null when the company had no scored news in the window — which is not
    # the same as news that was all neutral.
    news: NewsMixOut | None = None
    red_flags: list[RedFlagOut] = Field(default_factory=list)
    unknown_flags: list[str] = Field(default_factory=list)
    narrative: str | None = None
    # True when the signal was stored under another scoring config, so the
    # family contributions are re-derived with weights that did not produce it.
    stale: bool = False
    stored_version: str | None = None
    current_version: str | None = None

    @classmethod
    def of(cls, reasoning: explain.Explanation) -> ExplanationOut:
        signal = reasoning.signal
        news = reasoning.news
        return cls(
            isin=signal.isin,
            nse_symbol=signal.nse_symbol,
            name=signal.name,
            as_of=signal.as_of,
            signal=signal.signal,
            composite_score=signal.composite_score,
            coverage=signal.coverage,
            headline=reasoning.headline,
            reasons=reasoning.reasons,
            families=[FamilyOut.of(f) for f in reasoning.families],
            strengths=[DriverOut.of(d) for d in reasoning.strengths],
            weaknesses=[DriverOut.of(d) for d in reasoning.weaknesses],
            news=(
                None if news is None
                else NewsMixOut(
                    positive=news.positive,
                    negative=news.negative,
                    neutral=news.neutral,
                    total=news.total,
                )
            ),
            red_flags=[
                RedFlagOut(name=flag, description=_FLAG_DESCRIPTIONS.get(flag))
                for flag in signal.red_flags
            ],
            unknown_flags=signal.unknown_flags,
            narrative=signal.narrative,
            stale=reasoning.stale,
            stored_version=reasoning.stored_version,
            current_version=reasoning.current_version,
        )


ScoreReason = Literal[
    "scored",
    "not_in_latest_run",
    "below_coverage_floor",
    "missing_fundamentals",
    "sector_not_supported",
    "never_scored",
]


class ScoreStatusOut(BaseModel):
    """Whether a company has a score and, when it does not, why."""

    isin: str
    scored: bool = Field(
        description="True when the newest run that measured the company stored "
        "a signal for it. False for a company no run has measured, and for one "
        "the newest run measured but could not score — even when an older "
        "signal exists (see latest_as_of)."
    )
    reason: ScoreReason = Field(
        description="scored: in the latest run. not_in_latest_run: scored, but "
        "the latest run did not measure it, so its score is older. "
        "below_coverage_floor: measured, but too little of the model's weight "
        "had data. missing_fundamentals: below the floor because no annual "
        "results were stored. sector_not_supported: below the floor because "
        "its results filings (banks, insurers) carry no figures the model's "
        "annual statements can be built from. never_scored: no run has "
        "measured it."
    )
    latest_as_of: dt.date | None = Field(
        default=None,
        description="Newest date with a signal row; null when there is none. "
        "Older than measured_as_of when a later run could not score it.",
    )
    measured_as_of: dt.date | None = Field(
        default=None,
        description="The run the reason, coverage and factor counts describe: "
        "the newest date any run stored anything for the company.",
    )
    coverage: float | None = Field(
        default=None, ge=0, le=1,
        description="Fraction (0-1) of the model's weight backed by data on "
        "measured_as_of. Null when no run has measured the company.",
    )
    coverage_floor: float = Field(
        description="Coverage below which the current model withholds a score, "
        "as a fraction (0-1). Older signals may have been stored under another "
        "floor; their model_version says which."
    )
    factors_measured: int | None = Field(
        default=None,
        description="Factors with a sector z-score on measured_as_of. Null when "
        "no factor rows were stored for that date.",
    )
    factors_total: int = Field(description="Factors in the current model.")
    detail: str | None = Field(
        default=None,
        description="One plain sentence for display, stating the measured share "
        "and what was missing.",
    )

    @classmethod
    def of(cls, row: explain.ScoreStatus) -> ScoreStatusOut:
        return cls(
            isin=row.isin,
            scored=row.scored,
            reason=row.reason,
            latest_as_of=row.latest_as_of,
            measured_as_of=row.measured_as_of,
            coverage=row.coverage,
            coverage_floor=row.coverage_floor,
            factors_measured=row.factors_measured,
            factors_total=row.factors_total,
            detail=row.detail,
        )


_RUPEES = "₹ per share, on the price series' share basis (see restated)."


class CorporateActionOut(BaseModel):
    ex_date: dt.date
    action_type: Literal["SPLIT", "BONUS", "DIVIDEND", "RIGHTS", "DEMERGER", "BUYBACK"]
    ratio: float | None = Field(
        default=None,
        description="SPLIT and BONUS: shares after per share before (a 1:1 bonus "
        "is 2.0, a ₹10 to ₹2 split 5.0). RIGHTS: new shares offered per share "
        "held. Null for the other types.",
    )
    amount: float | None = Field(
        default=None,
        description="DIVIDEND: ₹ per share on the share basis of the ex-date, "
        "summed when one announcement pays several. Null otherwise.",
    )
    record_date: dt.date | None = None
    details: str | None = Field(
        default=None, description="The exchange's own wording of the action."
    )
    source: str | None = Field(default=None, description="NSE.")



class KeyMetricsOut(BaseModel):
    """Valuation on the value factors' own share count and price basis.

    Taken at the newest stored close and read only from filings made by then.
    `pe_annual` is 1 / earnings_yield and `pb` 1 / book_to_price, so the page
    cannot contradict the score. Every per-share figure is restated onto the
    share basis of the price series across splits and bonuses after its
    filing. Null always means unknown or not meaningful, never zero.
    """

    as_of: dt.date | None = Field(
        default=None,
        description="The close every figure is taken at; filings after it are "
        "ignored. Null when the company has no prices.",
    )
    price: float | None = Field(
        default=None,
        description="Close on as_of, ₹. The stored series is split- and "
        "bonus-adjusted to the share basis it was fetched on.",
    )
    shares_outstanding_cr: float | None = Field(
        default=None, description="Shares outstanding, crore, on the price basis."
    )
    share_source: Literal["stated", "implied"] | None = Field(
        default=None,
        description="stated: paid-up capital over face value, from the newest "
        "filing that tags them. implied: PAT / EPS of the latest annual results, "
        "where no filing states the count.",
    )
    shares_filing_date: dt.date | None = Field(
        default=None, description="Filing date of the share count's source."
    )
    market_cap_cr: float | None = Field(default=None, description="price × shares, ₹ crore.")
    eps_ttm: float | None = Field(
        default=None,
        description=_RUPEES + " Sum of the four newest quarters; null unless "
        "four consecutive quarters with EPS are filed.",
    )
    pe_ttm: float | None = Field(
        default=None, description="price / eps_ttm. Null when EPS is zero or negative."
    )
    ttm_through: dt.date | None = Field(
        default=None, description="Period end of the newest quarter in eps_ttm."
    )
    ttm_filing_date: dt.date | None = Field(
        default=None, description="Filing date of the newest quarter in eps_ttm."
    )
    eps_annual: float | None = Field(
        default=None,
        description=_RUPEES + " Latest annual EPS — the figure earnings_yield uses.",
    )
    pe_annual: float | None = Field(
        default=None,
        description="price / eps_annual, the inverse of earnings_yield. Null when "
        "EPS is zero or negative.",
    )
    annual_fiscal_year: int | None = None
    annual_basis: str | None = Field(
        default=None,
        description="CONSOLIDATED where the company files it, else STANDALONE.",
    )
    annual_filing_date: dt.date | None = Field(
        default=None, description="Filing date of the annual results used."
    )
    book_value_per_share: float | None = Field(
        default=None, description=_RUPEES + " Total equity / shares; can be negative."
    )
    pb: float | None = Field(
        default=None,
        description="price / book_value_per_share, the inverse of book_to_price. "
        "Null when book value is zero or negative.",
    )
    dividends_ttm: float | None = Field(
        default=None,
        description=_RUPEES + " Dividends with an ex-date in the year to as_of, "
        "from NSE corporate actions. Null when none were ever fetched for the "
        "company; 0 when fetched and none paid.",
    )
    dividend_yield: float | None = Field(
        default=None, description="dividends_ttm / price, a fraction (0.0051 is 0.51%)."
    )
    high_52w: float | None = Field(
        default=None, description="Highest high in the year to as_of, ₹."
    )
    low_52w: float | None = Field(
        default=None, description="Lowest low in the year to as_of, ₹."
    )

    @classmethod
    def of(cls, row: queries.KeyMetrics) -> KeyMetricsOut:
        return cls(**dataclasses.asdict(row))


_PCT = "Percent of total shares, 0-100 (not a fraction)."


class ShareholdingOut(BaseModel):
    quarter_end: dt.date
    disclosed_date: dt.date | None = Field(
        default=None,
        description="When the pattern became public — its knowledge date. See "
        "disclosed_date_source for whether it was observed or assumed.",
    )
    disclosed_date_source: str | None = Field(
        default=None,
        description="NSE: the exchange's own disclosure date. "
        "ASSUMED_LODR_DEADLINE: not reported, so the regulatory deadline "
        "(21 days after quarter end) stands in for it.",
    )
    promoter_pct: float | None = Field(default=None, description=_PCT)
    promoter_pledged_pct: float | None = Field(
        default=None,
        description="Pledged shares as a percent of the promoter holding. Null "
        "means unknown — the shareholding feed carries no pledge figure — never "
        "zero pledged.",
    )
    fii_pct: float | None = Field(
        default=None, description=_PCT + " Null where the source did not break it out."
    )
    dii_pct: float | None = Field(
        default=None, description=_PCT + " Null where the source did not break it out."
    )
    public_pct: float | None = Field(default=None, description=_PCT)
    employee_trust_pct: float | None = Field(default=None, description=_PCT)

    @classmethod
    def of(cls, row: queries.ShareholdingQuarter) -> ShareholdingOut:
        return cls(
            quarter_end=row.quarter_end,
            disclosed_date=row.disclosed_date,
            disclosed_date_source=row.disclosed_date_source,
            promoter_pct=row.promoter_pct,
            promoter_pledged_pct=row.promoter_pledged_pct,
            fii_pct=row.fii_pct,
            dii_pct=row.dii_pct,
            public_pct=row.public_pct,
            employee_trust_pct=row.employee_trust_pct,
        )


class PriceBarOut(BaseModel):
    date: dt.date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adj_close: float | None = None
    volume: int | None = None


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@route("/health", response_model=HealthOut, tags=["meta"])
def health(db: DB) -> HealthOut:
    """Liveness plus enough state to tell "empty" from "broken"."""
    counts = db.query(
        "SELECT (SELECT COUNT(*) FROM instruments) AS instruments, "
        "(SELECT COUNT(*) FROM signals) AS signals"
    )
    return HealthOut(
        status="ok",
        latest_as_of=queries.latest_as_of(db),
        instruments=int(counts.iloc[0]["instruments"]),
        signals=int(counts.iloc[0]["signals"]),
        latest_complete_as_of=queries.latest_complete_as_of(db),
    )


@route("/model", response_model=ModelInfoOut, tags=["meta"])
def model_info(db: DB) -> ModelInfoOut:
    """What the numbers mean: live weights and thresholds, not a transcription.

    The factor and flag lists are read from the model's own definitions, so a
    factor added in Python appears here without anyone copying it.
    """
    config = ScoringConfig()
    order = {name: i for i, name in enumerate(explain.FAMILY_ORDER)}
    return ModelInfoOut(
        family_weights=dict(FAMILY_WEIGHTS),
        buy_threshold=BUY_THRESHOLD,
        sell_threshold=SELL_THRESHOLD,
        scored_dates=queries.scored_dates(db),
        version=config.version(),
        coverage_floor=config.min_coverage,
        families=[
            FamilyInfoOut(
                family=family,
                meaning=explain.FAMILY_MEANING.get(family),
                weight=weight,
            )
            for family, weight in sorted(
                config.family_weights.items(),
                key=lambda kv: order.get(kv[0], len(order)),
            )
        ],
        factors=[
            FactorInfoOut(
                name=f.name,
                label=explain.factor_label(f.name),
                meaning=explain.factor_meaning(f.name),
                family=f.family,
                unit=explain.FACTOR_UNITS[f.name],
                higher_is_better=f.higher_is_better,
            )
            for f in default_factors()
        ],
        red_flags=[
            RedFlagInfoOut(name=d.name, description=d.description, reachable=d.reachable)
            for d in redflags.DEFINITIONS
        ],
        scored_date_counts=[
            ScoredDateCountOut(as_of=c.as_of, scored=c.scored, instruments=c.instruments)
            for c in queries.scored_date_counts(db)
        ],
    )


@route("/sectors", response_model=list[str], tags=["instruments"])
def list_sectors(db: DB) -> list[str]:
    return queries.sectors(db)


@route("/instruments", response_model=list[InstrumentOut], tags=["instruments"])
def list_instruments(
    db: DB,
    sector: Annotated[str | None, Query(description="Exact sector name")] = None,
) -> list[InstrumentOut]:
    return [InstrumentOut.of(row) for row in queries.list_instruments(db, sector)]


# Routes are matched in declaration order, but only among routes with the same
# number of path segments — /instruments/{isin} cannot capture this three-segment
# path however it is ordered. Literal-before-dynamic matters for a same-shape
# sibling, and /instruments/search below is one: declared after
# /instruments/{isin}, "search" would be read as an ISIN and 404. There is a
# test asserting no dynamic route precedes a literal one of the same shape.
@route(
    "/instruments/by-symbol/{symbol}",
    response_model=InstrumentOut,
    tags=["instruments"],
)
def get_by_symbol(db: DB, symbol: Annotated[str, Path()]) -> InstrumentOut:
    """Resolve an NSE symbol to an instrument. Symbols are what humans type."""
    row = queries.resolve_symbol(db, symbol)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No instrument for symbol {symbol!r}")
    return InstrumentOut.of(row)


@route("/instruments/search", response_model=list[InstrumentOut], tags=["instruments"])
def search_instruments(
    db: DB,
    q: Annotated[str, Query(description="Part of a symbol or company name")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[InstrumentOut]:
    """Companies matching what is being typed: exact symbol, then symbol
    prefix, then name prefix, then anywhere in either. Case-insensitive."""
    if not q.strip():
        raise HTTPException(status_code=422, detail="q must not be blank")
    return [InstrumentOut.of(row) for row in queries.search_instruments(db, q, limit)]


@route("/instruments/{isin}", response_model=InstrumentOut, tags=["instruments"])
def get_instrument(db: DB, isin: Annotated[str, Path()]) -> InstrumentOut:
    return InstrumentOut.of(require_instrument(db, isin))


@route(
    "/instruments/{isin}/latest",
    response_model=SignalDetailOut,
    tags=["signals"],
)
def latest_signal(db: DB, isin: Annotated[str, Path()]) -> SignalDetailOut:
    """Most recent signal for one instrument, with its factor breakdown."""
    require_instrument(db, isin)
    row = queries.latest_signal(db, isin)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No signal stored for {isin!r}")

    return SignalDetailOut.detailed(row, queries.factor_breakdown(db, isin, row.as_of))


@route(
    "/instruments/{isin}/history",
    response_model=list[HistoryPointOut],
    tags=["signals"],
)
def signal_history(
    db: DB,
    isin: Annotated[str, Path()],
    limit: Annotated[int, Query(ge=1, le=500)] = 60,
) -> list[HistoryPointOut]:
    """Signal history, oldest first, with each day's family breakdown.

    Empty list when the instrument has none.
    """
    require_instrument(db, isin)
    rows = queries.signal_history(db, isin, limit)
    families = explain.family_history(db, isin, [row.as_of for row in rows])
    return [
        HistoryPointOut(
            **SignalOut.of(row).model_dump(),
            families=[
                FamilyPointOut(
                    family=f.family, percentile=f.percentile, contribution=f.contribution
                )
                for f in families.get(row.as_of, [])
            ],
        )
        for row in rows
    ]


@route(
    "/instruments/{isin}/explanation",
    response_model=ExplanationOut,
    tags=["signals"],
)
def signal_explanation(
    db: DB,
    isin: Annotated[str, Path()],
    as_of: Annotated[
        str | None,
        Query(
            description=(
                "YYYY-MM-DD. Explain the signal stored on exactly this date; "
                "defaults to the latest. A date with no stored signal is a 404, "
                "never the nearest date's."
            )
        ),
    ] = None,
) -> ExplanationOut:
    """Why a signal is what it is: the families that carried it, the factors
    for and against, the news mix, and any red flag that forced it.

    Rebuilt from that date's stored factor scores, never re-scored, so it
    describes the number the other routes return — see `serve.explain`. A past
    date stored under another scoring config is `stale`, exactly as the latest
    one would be.
    """
    require_instrument(db, isin)
    date = parse_as_of(as_of)
    reasoning = explain.explain(db, isin, date)
    if reasoning is None:
        on = "" if date is None else f" on {date}"
        raise HTTPException(status_code=404, detail=f"No signal stored for {isin!r}{on}")
    return ExplanationOut.of(reasoning)


@route(
    "/instruments/{isin}/score-status",
    response_model=ScoreStatusOut,
    tags=["signals"],
)
def score_status(db: DB, isin: Annotated[str, Path()]) -> ScoreStatusOut:
    """Why a company is or isn't scored. 200 for any known ISIN, 404 otherwise.

    `/latest` answers 404 for a company with no signal; this says which of
    "never measured", "below the coverage floor" and "the filings can't be read"
    it is, from what the runs stored.
    """
    require_instrument(db, isin)
    return ScoreStatusOut.of(explain.score_status(db, isin))


@route(
    "/instruments/{isin}/financials",
    response_model=FinancialsOut,
    tags=["financials"],
)
def financials(
    db: DB,
    isin: Annotated[str, Path()],
    quarters: Annotated[int, Query(ge=1, le=40)] = 12,
) -> FinancialsOut:
    """Reported figures, newest first, in ₹ crore except EPS.

    Every stored year comes back whatever its basis. A company that moved from
    standalone to consolidated shows the move rather than losing a year, and a
    client computing growth across it should say so — see
    `queries.annual_financials`. Empty lists, not a 404, for a listed company
    with nothing filed yet.
    """
    require_instrument(db, isin)
    return FinancialsOut(
        line_items=LINE_ITEMS,
        annual=[AnnualOut(**vars(row)) for row in queries.annual_financials(db, isin)],
        quarterly=[
            QuarterOut(**vars(row))
            for row in queries.quarterly_financials(db, isin, quarters)
        ],
    )


@route(
    "/instruments/{isin}/prices",
    response_model=list[PriceBarOut],
    tags=["prices"],
)
def price_history(
    db: DB,
    isin: Annotated[str, Path()],
    start: Annotated[
        str | None,
        Query(alias="from", description="YYYY-MM-DD; defaults to a year before `to`"),
    ] = None,
    end: Annotated[
        str | None,
        Query(alias="to", description="YYYY-MM-DD; defaults to the latest stored day"),
    ] = None,
) -> list[PriceBarOut]:
    """Daily bars, oldest first, both ends inclusive.

    The default window ends at the company's latest stored day, not today, so
    a chart opens on data even when prices have not been ingested for a while.
    """
    require_instrument(db, isin)
    start_date, end_date = parse_date(start, "from"), parse_date(end, "to")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=422, detail=f"from ({start_date}) is after to ({end_date})"
        )
    if end_date is None:
        end_date = queries.latest_price_date(db, isin)
        if end_date is None:
            return []
    if start_date is None:
        start_date = end_date - DEFAULT_PRICE_WINDOW
    return [
        PriceBarOut(**vars(bar))
        for bar in queries.price_history(db, isin, start_date, end_date)
    ]


@route(
    "/instruments/{isin}/corporate-actions",
    response_model=list[CorporateActionOut],
    tags=["instruments"],
)
def instrument_corporate_actions(
    db: DB, isin: Annotated[str, Path()]
) -> list[CorporateActionOut]:
    """Splits, bonuses, dividends and the rest, newest first.

    The splits and bonuses here are what eps_adjusted and key-metrics restate
    across. An empty list for a company also means its actions were never
    fetched; dividends_ttm in key-metrics tells the two apart.
    """
    require_instrument(db, isin)
    return [
        CorporateActionOut(**dataclasses.asdict(row))
        for row in queries.corporate_actions(db, isin)
    ]


@route(
    "/instruments/{isin}/key-metrics",
    response_model=KeyMetricsOut,
    tags=["instruments"],
)
def instrument_key_metrics(db: DB, isin: Annotated[str, Path()]) -> KeyMetricsOut:
    """Market cap, P/E, P/B and dividend yield, consistent with the value factors."""
    require_instrument(db, isin)
    return KeyMetricsOut.of(queries.key_metrics(db, isin))


@route(
    "/instruments/{isin}/shareholding",
    response_model=list[ShareholdingOut],
    tags=["instruments"],
)
def instrument_shareholding(
    db: DB,
    isin: Annotated[str, Path()],
    quarters: Annotated[int, Query(ge=1, le=80)] = 12,
) -> list[ShareholdingOut]:
    """Quarterly shareholding pattern, newest first.

    The context for the promoter_selling red flag: the falling quarters it
    fired on are the first rows here.
    """
    require_instrument(db, isin)
    return [ShareholdingOut.of(row) for row in queries.shareholding(db, isin, quarters)]


@route("/instruments/{isin}/news", response_model=list[NewsOut], tags=["news"])
def instrument_news(
    db: DB,
    isin: Annotated[str, Path()],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    relevant_only: Annotated[
        bool, Query(description="Only articles whose relevance is primary.")
    ] = False,
) -> list[NewsOut]:
    require_instrument(db, isin)
    return [
        NewsOut(**vars(item))
        for item in queries.recent_news(db, isin, limit, relevant_only=relevant_only)
    ]


# Kept above any future /signals/{...} route: a two-segment dynamic sibling
# declared first would swallow "red-flags" as its path parameter.
@route("/signals/red-flags", response_model=list[SignalOut], tags=["signals"])
def red_flag_signals(
    db: DB,
    as_of: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[SignalOut]:
    """Names with at least one tripped red flag — forced to SELL by the overlay."""
    rows = queries.signals_on(
        db, as_of=parse_as_of(as_of), flagged_only=True, limit=limit
    )
    return [SignalOut.of(row) for row in rows]


@route("/signals", response_model=list[SignalOut], tags=["signals"])
def list_signals(
    db: DB,
    signal: Annotated[str | None, Query(description="BUY, HOLD or SELL")] = None,
    sector: Annotated[str | None, Query()] = None,
    as_of: Annotated[
        str | None, Query(description="YYYY-MM-DD; defaults to the latest scored date")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    latest: Annotated[
        str | None,
        Query(
            description="per_instrument: each instrument's newest signal, whatever "
            "its date, ranked together; every row's as_of says which date it is "
            "from. Combines with signal and sector, not with as_of."
        ),
    ] = None,
) -> list[SignalOut]:
    """Signals for one date, best score first — or each company's newest."""
    date = parse_as_of(as_of)
    if latest is not None and latest != LATEST_PER_INSTRUMENT:
        raise HTTPException(
            status_code=422,
            detail=f"latest must be {LATEST_PER_INSTRUMENT!r}, got {latest!r}",
        )
    if latest is not None and date is not None:
        raise HTTPException(
            status_code=422,
            detail="latest=per_instrument takes each company's own date; drop as_of",
        )
    rows = queries.signals_on(
        db,
        as_of=date,
        signal=validate_signal(signal),
        sector=sector,
        limit=limit,
        per_instrument=latest is not None,
    )
    return [SignalOut.of(row) for row in rows]


# ----------------------------------------------------------------------
# Caching, CORS
# ----------------------------------------------------------------------


@app.middleware("http")
async def cache_control(request: Request, call_next):
    response = await call_next(request)
    seconds = settings.api_cache_seconds
    if request.method == "GET" and 200 <= response.status_code < 300 and seconds > 0:
        response.headers["Cache-Control"] = f"public, max-age={seconds}"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


class ConfiguredCORS:
    """Starlette's CORS, following `settings.api_cors_origins` per request.

    `CORSMiddleware` fixes its origins when it is constructed, and the app is
    constructed at import, before a test or anything else could change the
    setting. So one is built per distinct origin list on demand — keyed the way
    `db.postgres` keys its pools — and with no origins configured the request
    passes straight through and no CORS header is ever sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._built: dict[tuple[str, ...], CORSMiddleware] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        origins = tuple(settings.api_cors_origins)
        if not origins or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cors = self._built.get(origins)
        if cors is None:
            # Read-only API: GET is the only method a page needs, and no
            # credentials — nothing here is behind a login.
            cors = CORSMiddleware(self.app, allow_origins=list(origins), allow_methods=["GET"])
            self._built[origins] = cors
        await cors(scope, receive, send)


# Added last, so outermost: a preflight is answered before it reaches routing,
# where an OPTIONS request to a GET route would be a 405.
app.add_middleware(ConfiguredCORS)


if __name__ == "__main__":   # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
