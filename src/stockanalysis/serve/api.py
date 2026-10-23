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

import datetime as dt
from collections.abc import Iterator
from typing import Annotated

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
from stockanalysis.factors.composite import BUY_THRESHOLD, FAMILY_WEIGHTS, SELL_THRESHOLD
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

    @classmethod
    def of(cls, row: queries.FactorScore) -> FactorOut:
        return cls(
            factor_name=row.factor_name,
            raw_value=row.raw_value,
            sector_zscore=row.sector_zscore,
        )


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


class HealthOut(BaseModel):
    status: str
    latest_as_of: dt.date | None = None
    instruments: int = 0
    signals: int = 0


class ModelInfoOut(BaseModel):
    family_weights: dict[str, float]
    buy_threshold: float
    sell_threshold: float
    scored_dates: list[dt.date]


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


class QuarterOut(BaseModel):
    period_end: dt.date
    filing_date: dt.date | None = None
    relating_to: str | None = None
    is_consolidated: bool | None = None
    revenue: float | None = None
    pat: float | None = None
    eps: float | None = None


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


class DriverOut(BaseModel):
    name: str
    label: str
    meaning: str
    family: str
    raw_value: float | None = None
    # Sign-adjusted: positive is good for the company whichever way the
    # underlying metric runs. See `serve.explain`.
    z: float | None = None

    @classmethod
    def of(cls, row: explain.FactorDriver) -> DriverOut:
        return cls(
            name=row.name,
            label=row.label,
            meaning=row.meaning,
            family=row.family,
            raw_value=row.raw_value,
            z=row.z,
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
    )


@route("/model", response_model=ModelInfoOut, tags=["meta"])
def model_info(db: DB) -> ModelInfoOut:
    """What the numbers mean: live weights and thresholds, not a transcription."""
    return ModelInfoOut(
        family_weights=dict(FAMILY_WEIGHTS),
        buy_threshold=BUY_THRESHOLD,
        sell_threshold=SELL_THRESHOLD,
        scored_dates=queries.scored_dates(db),
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
    response_model=list[SignalOut],
    tags=["signals"],
)
def signal_history(
    db: DB,
    isin: Annotated[str, Path()],
    limit: Annotated[int, Query(ge=1, le=500)] = 60,
) -> list[SignalOut]:
    """Signal history, oldest first. Empty list when the instrument has none."""
    require_instrument(db, isin)
    return [SignalOut.of(row) for row in queries.signal_history(db, isin, limit)]


@route(
    "/instruments/{isin}/explanation",
    response_model=ExplanationOut,
    tags=["signals"],
)
def signal_explanation(db: DB, isin: Annotated[str, Path()]) -> ExplanationOut:
    """Why the latest signal is what it is: the families that carried it, the
    factors for and against, the news mix, and any red flag that forced it.

    Rebuilt from the stored factor scores, never re-scored, so it describes the
    number the other routes return — see `serve.explain`.
    """
    require_instrument(db, isin)
    reasoning = explain.explain(db, isin)
    if reasoning is None:
        raise HTTPException(status_code=404, detail=f"No signal stored for {isin!r}")
    return ExplanationOut.of(reasoning)


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


@route("/instruments/{isin}/news", response_model=list[NewsOut], tags=["news"])
def instrument_news(
    db: DB,
    isin: Annotated[str, Path()],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[NewsOut]:
    require_instrument(db, isin)
    return [NewsOut(**vars(item)) for item in queries.recent_news(db, isin, limit)]


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
) -> list[SignalOut]:
    """Signals for one date, best score first."""
    rows = queries.signals_on(
        db,
        as_of=parse_as_of(as_of),
        signal=validate_signal(signal),
        sector=sector,
        limit=limit,
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
