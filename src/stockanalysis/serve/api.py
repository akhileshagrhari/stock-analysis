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
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field

from stockanalysis.config import settings
from stockanalysis.db.database import (
    Database,
    DatabaseLockedError,
    SchemaOutOfDateError,
)
from stockanalysis.factors.composite import BUY_THRESHOLD, FAMILY_WEIGHTS, SELL_THRESHOLD
from stockanalysis.serve import queries

app = FastAPI(
    title="StockAnalysis API",
    description=(
        "Factor-based equity research for Indian markets. Scores are "
        "percentiles within the scored universe on each date, not absolute "
        "valuations."
    ),
    version="0.2.0",
)

SIGNALS = ("BUY", "HOLD", "SELL")


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------


def get_db() -> Iterator[Database]:
    """One read-only connection per request; overridden wholesale in tests."""
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


DB = Annotated[Database, Depends(get_db)]


def parse_as_of(as_of: str | None) -> dt.date | None:
    """A malformed date is the caller's mistake — 422, not a 500."""
    if as_of is None:
        return None
    try:
        return dt.date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"as_of must be YYYY-MM-DD, got {as_of!r}"
        ) from exc


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


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@app.get("/health", response_model=HealthOut, tags=["meta"])
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


@app.get("/model", response_model=ModelInfoOut, tags=["meta"])
def model_info(db: DB) -> ModelInfoOut:
    """What the numbers mean: live weights and thresholds, not a transcription."""
    return ModelInfoOut(
        family_weights=dict(FAMILY_WEIGHTS),
        buy_threshold=BUY_THRESHOLD,
        sell_threshold=SELL_THRESHOLD,
        scored_dates=queries.scored_dates(db),
    )


@app.get("/sectors", response_model=list[str], tags=["instruments"])
def list_sectors(db: DB) -> list[str]:
    return queries.sectors(db)


@app.get("/instruments", response_model=list[InstrumentOut], tags=["instruments"])
def list_instruments(
    db: DB,
    sector: Annotated[str | None, Query(description="Exact sector name")] = None,
) -> list[InstrumentOut]:
    return [InstrumentOut.of(row) for row in queries.list_instruments(db, sector)]


# Routes are matched in declaration order, but only among routes with the same
# number of path segments — /instruments/{isin} cannot capture this three-segment
# path however it is ordered. Literal-before-dynamic still matters the moment
# anyone adds a same-shape sibling (a /instruments/{isin} next to a literal
# /instruments/all, say), so the ordering here is kept deliberately and there is
# a test asserting no dynamic route precedes a literal one of the same shape.
@app.get(
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


@app.get("/instruments/{isin}", response_model=InstrumentOut, tags=["instruments"])
def get_instrument(db: DB, isin: Annotated[str, Path()]) -> InstrumentOut:
    row = queries.get_instrument(db, isin)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No instrument with ISIN {isin!r}")
    return InstrumentOut.of(row)


@app.get(
    "/instruments/{isin}/latest",
    response_model=SignalDetailOut,
    tags=["signals"],
)
def latest_signal(db: DB, isin: Annotated[str, Path()]) -> SignalDetailOut:
    """Most recent signal for one instrument, with its factor breakdown."""
    if queries.get_instrument(db, isin) is None:
        raise HTTPException(status_code=404, detail=f"No instrument with ISIN {isin!r}")

    row = queries.latest_signal(db, isin)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No signal stored for {isin!r}")

    return SignalDetailOut.detailed(row, queries.factor_breakdown(db, isin, row.as_of))


@app.get(
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
    if queries.get_instrument(db, isin) is None:
        raise HTTPException(status_code=404, detail=f"No instrument with ISIN {isin!r}")
    return [SignalOut.of(row) for row in queries.signal_history(db, isin, limit)]


@app.get("/instruments/{isin}/news", response_model=list[NewsOut], tags=["news"])
def instrument_news(
    db: DB,
    isin: Annotated[str, Path()],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> list[NewsOut]:
    if queries.get_instrument(db, isin) is None:
        raise HTTPException(status_code=404, detail=f"No instrument with ISIN {isin!r}")
    return [NewsOut(**vars(item)) for item in queries.recent_news(db, isin, limit)]


# Kept above any future /signals/{...} route: a two-segment dynamic sibling
# declared first would swallow "red-flags" as its path parameter.
@app.get("/signals/red-flags", response_model=list[SignalOut], tags=["signals"])
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


@app.get("/signals", response_model=list[SignalOut], tags=["signals"])
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


if __name__ == "__main__":   # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
