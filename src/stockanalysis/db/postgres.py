"""Postgres plumbing shared by `db.publish` and the API.

Postgres is the *serving* copy: the API reads it, `stockanalysis publish`
rebuilds it from DuckDB, and nothing else touches it. This module holds the two
pieces both sides need — a guarded way to reach the driver, and the adapter
that lets `serve.queries` run unchanged against the published copy.

**The adapter, not a second query layer.** `serve.queries` calls
`db.query(sql, params)` with DuckDB's `?` placeholders and reads the result as
a DataFrame. `PostgresReader` provides exactly that method over a psycopg
connection, so the SQL stays written once and the two backends cannot drift
into answering the same question two ways. What it has to reconcile:

- Placeholders. psycopg wants `%s`, and treats every other `%` in the text as
  formatting — see `to_pyformat`.
- NULL in a float column. DuckDB hands pandas NaN; psycopg hands back None,
  and an all-None column arrives as `object`. Float columns are coerced to
  float64 so NULL is NaN on both sides, which is what lets `explain` do its
  arithmetic over a pivot of either.
- NUMERIC. Postgres returns `Decimal` for it (an aggregate over an integer
  column, say) where DuckDB returns a float. It is coerced with the float
  columns, so a future query does not change type depending on the backend.

Dates, timestamps, booleans and strings come back as plain Python objects —
DuckDB returns pandas types for the same columns — and the coercion helpers in
`serve.queries` already accept both, which the parity tests check route by
route.

psycopg is an optional dependency (the `postgres` extra). Nothing here imports
it at module level, so the DuckDB-only installation keeps working, and the
"not installed" case surfaces as the same `PostgresUnavailable` as "not
configured" — both mean "there is no Postgres to talk to", which a caller
reports and moves past rather than treating as a failure.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

import pandas as pd

from stockanalysis.config import settings


class PostgresUnavailable(RuntimeError):
    """No Postgres to talk to: SA_PG_DSN is unset, or psycopg is not installed.

    Not a failure of the data or the server. The publish step reports it as
    SKIPPED and the API as a 503 that says what to configure.
    """


class ServingUnavailable(RuntimeError):
    """Postgres is configured but cannot answer this request right now."""


def require_psycopg():
    """The psycopg module, or `PostgresUnavailable` saying how to install it."""
    try:
        import psycopg
    except ImportError as exc:
        raise PostgresUnavailable(
            "psycopg is not installed. Install the extra: "
            "`uv pip install -e '.[postgres]'`."
        ) from exc
    return psycopg


def configured_dsn() -> str:
    if settings.pg_dsn is None:
        raise PostgresUnavailable(
            "Postgres is not configured — SA_PG_DSN is unset (see .env.example)."
        )
    return settings.pg_dsn.get_secret_value()


def is_configured() -> bool:
    return settings.pg_dsn is not None


# ----------------------------------------------------------------------
# The read adapter
# ----------------------------------------------------------------------


@lru_cache(maxsize=1024)
def to_pyformat(sql: str) -> str:
    """Rewrite DuckDB's `?` placeholders as psycopg's `%s`.

    psycopg scans the whole statement for `%` — string literals included — so
    a literal percent sign has to be doubled wherever it appears, while a `?`
    is a placeholder only outside quotes and comments. A character-level scan
    rather than a regex, because both conditions depend on what came before.
    Cached: the read layer issues the same few dozen statements over and over.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            # A doubled quote inside a literal closes and reopens it, which
            # this handles without special-casing: the scan simply re-enters.
            end = sql.find(ch, i + 1)
            end = n if end == -1 else end + 1
            out.append(sql[i:end].replace("%", "%%"))
            i = end
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(sql[i:end].replace("%", "%%"))
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(sql[i:end].replace("%", "%%"))
            i = end
        else:
            out.append("%s" if ch == "?" else "%%" if ch == "%" else ch)
            i += 1
    return "".join(out)


# float4, float8, numeric.
_FLOAT_OIDS = frozenset({700, 701, 1700})


class PostgresReader:
    """The part of `Database` the read layer uses — `query` and `close` — on Postgres.

    Holds a connection it does not own: the pool lends it for one request and
    takes it back, so `close` is a no-op rather than a disconnect.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        params = list(params or [])
        with self.conn.cursor() as cur:
            # Without parameters psycopg leaves `%` alone, so the statement is
            # sent as written; translating it would double a literal percent.
            if params:
                cur.execute(to_pyformat(sql), params)
            else:
                cur.execute(sql)
            if cur.description is None:
                return pd.DataFrame()
            frame = pd.DataFrame.from_records(
                cur.fetchall(), columns=[col.name for col in cur.description]
            )
            for position, col in enumerate(cur.description):
                if col.type_code in _FLOAT_OIDS:
                    # By position: a join can legitimately return two columns
                    # with the same name.
                    frame.isetitem(position, frame.iloc[:, position].astype("float64"))
        return frame

    def close(self) -> None:
        """The pool owns the connection; returning it is the pool's job."""


# ----------------------------------------------------------------------
# The API's pool
# ----------------------------------------------------------------------

# Keyed on what a pool is built from, so a changed setting gets a new pool
# rather than silently reusing one pointed at the old schema — which is also
# what lets tests switch schemas without reaching in to reset anything.
_pools: dict[tuple[str, str, int, float], Any] = {}
_pools_lock = threading.Lock()


def _configure_reader(schema: str):
    def configure(conn: Any) -> None:
        from psycopg import sql

        # The SQL in `serve.queries` is unqualified, so the search path is what
        # points it at the live release. A publish renames a new release into
        # this name, and the next statement on this same connection resolves
        # to it — no reconnect, no pool recycle.
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        # The DuckDB side opens read_only=True; this is the same guarantee.
        conn.execute("SET default_transaction_read_only = on")

    return configure


def reader_pool() -> Any:
    """The process-wide pool for the configured DSN and schema, opened on first use."""
    # Configuration first: "SA_PG_DSN is unset" is the more useful thing to
    # be told on an installation that also lacks the driver.
    dsn = configured_dsn()
    require_psycopg()
    from psycopg_pool import ConnectionPool

    key = (
        dsn,
        settings.pg_schema,
        settings.pg_pool_max_size,
        settings.pg_pool_timeout_seconds,
    )
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=settings.pg_pool_max_size,
                timeout=settings.pg_pool_timeout_seconds,
                # Autocommit: every statement is its own READ COMMITTED
                # transaction with a fresh snapshot. A request-wide REPEATABLE
                # READ snapshot would be wrong here, not merely slower: a
                # release swapped in after the snapshot was taken was loaded by
                # a transaction the snapshot cannot see, so its tables would
                # read as empty.
                #
                # No server-side prepared statements: transaction poolers
                # (PgBouncer, Neon, Supabase) do not carry them between
                # connections, and the API's statements are small enough that
                # planning them is not where the time goes.
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": None,
                    "application_name": "stockanalysis-api",
                },
                configure=_configure_reader(settings.pg_schema),
                # One round trip per checkout, so a connection killed by a
                # server restart is replaced instead of failing a request.
                check=ConnectionPool.check_connection,
                name="stockanalysis-api",
                open=True,
            )
            _pools[key] = pool
            atexit.register(pool.close)
        return pool


def close_reader_pools() -> None:
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


@contextmanager
def pooled_reader() -> Iterator[PostgresReader]:
    """A `PostgresReader` on a pooled connection, for the length of one request.

    Every way the published copy can be unable to answer becomes
    `ServingUnavailable` with the reason, which the API turns into a 503 — the
    same treatment `get_db` gives a missing or locked DuckDB file.
    """
    pool = reader_pool()
    psycopg = require_psycopg()
    from psycopg_pool import PoolTimeout

    try:
        conn = pool.getconn()
    except PoolTimeout as exc:
        raise ServingUnavailable(
            f"No Postgres connection within {settings.pg_pool_timeout_seconds:g}s — "
            f"is the server up, and SA_PG_DSN correct?"
        ) from exc
    try:
        yield PostgresReader(conn)
    except psycopg.errors.UndefinedTable as exc:
        raise ServingUnavailable(
            f"Schema {settings.pg_schema!r} is missing a table this route reads. "
            f"Either nothing has been published yet (run `stockanalysis publish`) "
            f"or the route reads a table `db.publish.SITE_TABLES` does not copy. "
            f"({exc.diag.message_primary})"
        ) from exc
    except psycopg.OperationalError as exc:
        raise ServingUnavailable(f"Lost the Postgres connection: {exc}") from exc
    finally:
        pool.putconn(conn)
