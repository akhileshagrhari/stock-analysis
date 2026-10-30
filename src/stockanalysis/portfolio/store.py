"""Saved holdings, per signed-in user, in the app schema.

Every function takes a connection and leaves the transaction to the caller
(`app_connection()` commits on a clean exit and rolls back on error), so a
save that is refused or fails halfway leaves the saved portfolio as it was.
Every query filters on `user_id`: one user never sees another's rows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from psycopg import sql

from stockanalysis.config import settings
from stockanalysis.portfolio.kite import LIMIT, ImportRefused, ParsedRow
from stockanalysis.portfolio.lots import combine

_PLACES = Decimal("0.0001")

Mode = Literal["replace", "add"]


@dataclass(frozen=True)
class StoredHolding:
    symbol: str
    isin: str | None
    quantity: Decimal
    avg_cost: Decimal
    file_price: Decimal | None
    uploaded_at: dt.datetime


@dataclass(frozen=True)
class ImportRecord:
    id: int
    filename: str
    mode: Mode
    rows: int
    matched: int
    created_at: dt.datetime


_DDL = """
CREATE TABLE IF NOT EXISTS {s}.imports (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id TEXT NOT NULL,
  filename TEXT NOT NULL, format TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('replace','add')),
  row_count INTEGER NOT NULL, matched INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS {s}.holdings (
  user_id TEXT NOT NULL, key TEXT NOT NULL, symbol TEXT NOT NULL, isin TEXT,
  quantity NUMERIC(20,4) NOT NULL CHECK (quantity > 0),
  avg_cost NUMERIC(20,4) NOT NULL CHECK (avg_cost >= 0),
  file_price NUMERIC(20,4), uploaded_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (user_id, key));
CREATE INDEX IF NOT EXISTS imports_user ON {s}.imports (user_id, id DESC);
"""


def _s() -> sql.Identifier:
    return sql.Identifier(settings.pg_app_schema)


def ensure_schema(conn: Any) -> None:
    """Create the tables if missing. Idempotent."""
    conn.execute(sql.SQL(_DDL).format(s=_s()))


def _lock_user(conn: Any, user_id: str) -> None:
    """Serialize writers for one user until the transaction ends.

    Row locks can't cover a row that doesn't exist yet, so two simultaneous
    Adds would otherwise overwrite rather than sum.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [user_id])


def _too_large(symbol: str) -> ImportRefused:
    return ImportRefused(f"{symbol.upper()} adds up to too large a holding.")


def _merge(
    qty: Decimal, cost: Decimal, q: Decimal, c: Decimal, symbol: str
) -> tuple[Decimal, Decimal]:
    try:
        nq, nc = combine(qty, cost, q, c)
    except InvalidOperation:
        raise _too_large(symbol) from None
    if nq >= LIMIT or nc >= LIMIT:
        raise _too_large(symbol)
    return nq, nc


def save_import(
    conn: Any,
    user_id: str,
    rows: list[ParsedRow],
    isins: dict[str, str],
    mode: Mode,
    filename: str,
    now: dt.datetime,
) -> ImportRecord:
    """Save one upload: replace the portfolio, or add to it.

    `isins` maps an upper-case symbol to its ISIN. A refused add raises
    `ImportRefused`; the caller's transaction then rolls back.
    """
    s = _s()
    # Refuse what can't be stored before writing anything.
    for row in rows:
        if row.quantity.quantize(_PLACES, rounding=ROUND_HALF_UP) <= 0:
            raise ImportRefused(f"{row.symbol.upper()}: quantity is too small.")
    _lock_user(conn, user_id)
    if mode == "replace":
        conn.execute(sql.SQL("DELETE FROM {s}.holdings WHERE user_id = %s").format(s=s), [user_id])

    matched = 0
    for row in rows:
        isin = isins.get(row.symbol.upper())
        key = isin or row.symbol.upper()
        if isin:
            matched += 1
        qty, cost = row.quantity, row.avg_cost
        if qty >= LIMIT or cost >= LIMIT:
            raise _too_large(row.symbol)
        # Saved rows this one folds into: the same key, and (once the symbol
        # has an ISIN) an earlier unmatched row for the same symbol.
        saved = conn.execute(
            sql.SQL(
                "SELECT key, quantity, avg_cost FROM {s}.holdings WHERE user_id = %s "
                "AND (key = %s OR (isin IS NULL AND upper(symbol) = %s)) "
                "ORDER BY key FOR UPDATE"
            ).format(s=s),
            [user_id, key, row.symbol.upper()],
        ).fetchall()
        for old_key, old_q, old_c in saved:
            qty, cost = _merge(old_q, old_c, qty, cost, row.symbol)
            if old_key != key:
                conn.execute(
                    sql.SQL("DELETE FROM {s}.holdings WHERE user_id = %s AND key = %s").format(s=s),
                    [user_id, old_key],
                )
        conn.execute(
            sql.SQL(
                "INSERT INTO {s}.holdings (user_id, key, symbol, isin, quantity, avg_cost, "
                "file_price, uploaded_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, key) DO UPDATE SET symbol = EXCLUDED.symbol, "
                "isin = EXCLUDED.isin, quantity = EXCLUDED.quantity, "
                "avg_cost = EXCLUDED.avg_cost, file_price = EXCLUDED.file_price, "
                "uploaded_at = EXCLUDED.uploaded_at, updated_at = EXCLUDED.updated_at"
            ).format(s=s),
            [user_id, key, row.symbol, isin, qty, cost, row.file_price, now, now],
        )

    rec = conn.execute(
        sql.SQL(
            "INSERT INTO {s}.imports (user_id, filename, format, mode, row_count, matched, "
            "created_at) VALUES (%s, %s, 'kite_csv', %s, %s, %s, %s) RETURNING id"
        ).format(s=s),
        [user_id, filename, mode, len(rows), matched, now],
    ).fetchone()
    return ImportRecord(rec[0], filename, mode, len(rows), matched, now)


def load_holdings(conn: Any, user_id: str) -> list[StoredHolding]:
    cur = conn.execute(
        sql.SQL(
            "SELECT symbol, isin, quantity, avg_cost, file_price, uploaded_at "
            "FROM {s}.holdings WHERE user_id = %s ORDER BY symbol, key"
        ).format(s=_s()),
        [user_id],
    )
    return [StoredHolding(*r) for r in cur.fetchall()]


def last_import(conn: Any, user_id: str) -> ImportRecord | None:
    r = conn.execute(
        sql.SQL(
            "SELECT id, filename, mode, row_count, matched, created_at "
            "FROM {s}.imports WHERE user_id = %s ORDER BY id DESC LIMIT 1"
        ).format(s=_s()),
        [user_id],
    ).fetchone()
    return ImportRecord(*r) if r else None


def delete_portfolio(conn: Any, user_id: str) -> int:
    """Remove the user's holdings and import log. Returns holdings removed."""
    _lock_user(conn, user_id)
    cur = conn.execute(
        sql.SQL("DELETE FROM {s}.holdings WHERE user_id = %s").format(s=_s()), [user_id]
    )
    removed = cur.rowcount
    conn.execute(sql.SQL("DELETE FROM {s}.imports WHERE user_id = %s").format(s=_s()), [user_id])
    return removed
