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
from stockanalysis.portfolio import store
from stockanalysis.portfolio.kite import MAX_BYTES, ImportRefused, parse_kite_holdings
from stockanalysis.serve import queries
from stockanalysis.serve.api import DB, app
from stockanalysis.serve.auth import CurrentUser

# GET templates that depend on who is asking. They read the unpublished `app`
# schema, so the DuckDB/Postgres parity test has nothing to compare.
PER_USER_PATHS: frozenset[str] = frozenset({"/v1/me"})


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
