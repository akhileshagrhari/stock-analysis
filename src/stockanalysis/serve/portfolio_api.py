"""The signed-in `/v1/me*` routes.

Imported at the end of `serve.api`, so these register on the same `app`. They
are mounted at `/v1/me*` only (no legacy root twin, unlike the public routes),
and the `cache_control` middleware marks every one `no-store`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from stockanalysis.config import settings
from stockanalysis.db.postgres import PostgresUnavailable, ServingUnavailable, app_connection
from stockanalysis.portfolio import store, view
from stockanalysis.portfolio.kite import MAX_BYTES, ImportRefused, parse_kite_holdings
from stockanalysis.serve import explain, queries
from stockanalysis.serve.api import DB, app
from stockanalysis.serve.auth import CurrentUser

# GET templates that depend on who is asking. They read the unpublished `app`
# schema, so the DuckDB/Postgres parity test has nothing to compare.
PER_USER_PATHS: frozenset[str] = frozenset({"/v1/me", "/v1/me/portfolio"})


class Me(BaseModel):
    user_id: str


@app.get("/v1/me", tags=["account"], response_model=Me)
def me(user_id: CurrentUser) -> Me:
    return Me(user_id=user_id)


def require_postgres() -> None:
    if settings.serve_backend != "postgres":
        raise HTTPException(status_code=503, detail="Portfolio needs Postgres.")


Postgres = Depends(require_postgres)

TOO_BIG = "The file is larger than 1 MB."


@contextmanager
def _unavailable_as_503() -> Iterator[None]:
    try:
        yield
    except (PostgresUnavailable, ServingUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _refusal(exc: ImportRefused) -> HTTPException:
    return HTTPException(
        status_code=422, detail={"message": exc.message, "problems": list(exc.problems)}
    )


async def _read_body(request: Request) -> bytes:
    """The raw body, refused as soon as it passes the size limit."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=TOO_BIG)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(status_code=413, detail=TOO_BIG)
        chunks.append(chunk)
    return b"".join(chunks)


class ImportOut(BaseModel):
    id: int
    filename: str
    mode: str
    rows: int
    matched: int
    created_at: dt.datetime


class DeletedOut(BaseModel):
    deleted: int


def _save(user_id, parsed, isins, mode, filename, now) -> store.ImportRecord:
    with _unavailable_as_503(), app_connection() as conn:
        return store.save_import(conn, user_id, parsed.rows, isins, mode, filename, now)


@app.post(
    "/v1/me/portfolio/import",
    tags=["account"],
    response_model=ImportOut,
    dependencies=[Postgres],
)
async def import_portfolio(
    request: Request,
    user_id: CurrentUser,
    db: DB,
    mode: Annotated[Literal["replace", "add"], Query()],
    filename: Annotated[str, Query(max_length=200)] = "holdings.csv",
) -> ImportOut:
    body = await _read_body(request)
    try:
        parsed = await run_in_threadpool(parse_kite_holdings, body)
        found = await run_in_threadpool(
            queries.resolve_symbols, db, [r.symbol for r in parsed.rows]
        )
        isins = {sym: inst.isin for sym, inst in found.items()}
        record = await run_in_threadpool(
            _save, user_id, parsed, isins, mode, filename, dt.datetime.now(dt.UTC)
        )
    except ImportRefused as exc:
        raise _refusal(exc) from exc
    return ImportOut(
        id=record.id, filename=record.filename, mode=record.mode, rows=record.rows,
        matched=record.matched, created_at=record.created_at,
    )


def _delete(user_id: str) -> int:
    with _unavailable_as_503(), app_connection() as conn:
        return store.delete_portfolio(conn, user_id)


@app.delete(
    "/v1/me/portfolio", tags=["account"], response_model=DeletedOut, dependencies=[Postgres]
)
async def delete_portfolio(user_id: CurrentUser) -> DeletedOut:
    return DeletedOut(deleted=await run_in_threadpool(_delete, user_id))


class HoldingOut(BaseModel):
    symbol: str
    isin: str | None
    name: str | None
    quantity: float
    avg_cost: float
    price: float | None
    price_date: dt.date | None
    price_source: Literal["site", "file"] | None
    value: float | None
    invested: float
    pnl: float | None
    pnl_pct: float | None
    weight: float | None
    group: view.Group
    score: float | None
    signal_as_of: dt.date | None
    older_rating: bool
    earlier_model: bool
    was_signal: str | None
    red_flags: list[str]
    not_rated_reason: str | None
    split_since_upload: bool


class GroupShareOut(BaseModel):
    group: view.Group
    holdings: int
    value: float
    share: float


class PortfolioOut(BaseModel):
    holdings: list[HoldingOut]
    groups: list[GroupShareOut]
    value: float
    invested: float
    pnl: float
    pnl_pct: float | None
    unpriced: int
    ratings_as_of: dt.date | None
    older_ratings: int
    prices_from: dt.date | None
    prices_to: dt.date | None
    last_import: ImportOut | None


def _load(user_id: str) -> tuple[list[store.StoredHolding], store.ImportRecord | None]:
    with _unavailable_as_503(), app_connection() as conn:
        return store.load_holdings(conn, user_id), store.last_import(conn, user_id)


def _lookups(db, holdings: list[store.StoredHolding]) -> view.Lookups:
    isins = sorted({h.isin for h in holdings if h.isin})
    instruments = queries.instruments_by_isin(db, isins)
    signals = queries.latest_signals(db, isins)
    runs = queries.complete_run_dates(db)
    as_of = max(runs, default=None)
    run_model = None
    if as_of is not None:
        on_date = queries.signals_on(db, as_of=as_of, limit=1)
        run_model = on_date[0].model_version if on_date else None
    reasons = {
        i: explain.score_status(db, i).detail
        for i in isins
        if i in instruments and i not in signals
    }
    return view.Lookups(
        instruments=instruments,
        closes=queries.latest_closes(db, isins),
        signals=signals,
        split_bonus=queries.split_bonus_dates(db, isins),
        complete_runs=runs,
        not_scored_reason=reasons,
        ratings_as_of=as_of,
        run_model=run_model,
    )


def _portfolio(user_id: str, db) -> PortfolioOut:
    holdings, last = _load(user_id)
    p = view.build_portfolio(holdings, _lookups(db, holdings))
    fields = {f: getattr(p, f) for f in PortfolioOut.model_fields if f != "last_import"}
    fields["holdings"] = [HoldingOut(**vars(h)) for h in p.holdings]
    fields["groups"] = [GroupShareOut(**vars(g)) for g in p.groups]
    imp = None
    if last is not None:
        imp = ImportOut(
            id=last.id, filename=last.filename, mode=last.mode, rows=last.rows,
            matched=last.matched, created_at=last.created_at,
        )
    return PortfolioOut(**fields, last_import=imp)


@app.get(
    "/v1/me/portfolio", tags=["account"], response_model=PortfolioOut, dependencies=[Postgres]
)
async def get_portfolio(user_id: CurrentUser, db: DB) -> PortfolioOut:
    return await run_in_threadpool(_portfolio, user_id, db)
