"""DuckDB → Postgres: publishing the tables the site serves.

DuckDB is the engine, and the only database the pipeline writes. Postgres is
what the API — and later the public site — reads: a copy of the tables the
read layer touches, rebuilt from scratch on every publish. Data flows one way.
Nothing edits Postgres in place, so it is a cache in the strict sense: drop it
and the next publish recreates it. That is also why a publish is always the
whole set of tables, even after a one-company run. A rebuild is safe to repeat
in a way an incremental patch never quite is.

**How a publish stays atomic.** Everything happens in one Postgres transaction.
The tables are loaded into a fresh schema, `<live>_release_<UTC timestamp>`:
create, COPY, primary keys and indexes, ANALYZE, and a row count checked
against the DuckDB snapshot the rows were read from. Only then, in the same
transaction, is the live schema renamed out of the way and the new one renamed
into its place. Postgres DDL is transactional, so a reader sees the old release
or the new one and never a half-loaded mix. Any failure before COMMIT — a bad
row, a lost connection, a killed process — rolls the whole thing back and
leaves the live schema exactly as it was.

Why a rename rather than the alternatives:

- Switching `search_path` only reaches *new* sessions. The API's pooled
  connections would go on serving the old release until they were recycled.
- A schema of views over the current release is atomic too. But `CREATE OR
  REPLACE VIEW` cannot change a view's columns, every view has to be repointed
  before an old release can be dropped, and it is one more layer between a
  query and its table.

A rename needs none of that, and the SQL in `serve.queries` stays unqualified.
Name resolution happens per statement, so the next query on an existing pooled
connection already reads the new release.

The release that was live is kept under its own name for `rollback`, which
swaps it back the same way. Older releases are dropped only after the switch
has committed, so a slow DROP can never hold the switch up or undo it.

**Why psycopg's COPY rather than DuckDB's postgres extension.** The extension
would make the copy a one-liner, but it is downloaded from DuckDB's extension
repository on first use and tied to the DuckDB version, which hides a network
dependency inside every publish. It also picks its own Postgres column types,
and this needs `text COLLATE "C"` (below). And the transaction that makes the
switch atomic is easier to follow, and to test by injecting a failure, when
every statement runs on one psycopg connection held here. The API needs psycopg
for its pool anyway, so this adds no dependency.

**Why `COLLATE "C"` on every text column.** DuckDB compares strings byte by
byte, and the read layer's orderings (`ORDER BY nse_symbol`, `ORDER BY
sector`) were written against that. A database created with a locale
collation such as en_US.UTF-8, which is the Homebrew and most managed-service
default, ignores punctuation and so sorts `M&M` after `MARUTI`; DuckDB sorts it
before. Pinning the collation on the column makes the copy order exactly like
the engine, whatever locale the server was set up with.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.db.postgres import configured_dsn, require_psycopg

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """A publish or rollback refused to proceed. The live schema is unchanged."""


class PublishBusy(PublishError):
    """Another publish or rollback holds the lock for this schema."""


@dataclass(frozen=True)
class SiteTable:
    """One DuckDB table the site reads, copied as-is under the same name.

    Only secondary indexes are declared here. Primary keys are read from
    DuckDB's own catalog, so the copy cannot come to disagree with the engine
    about what identifies a row.
    """

    name: str
    indexes: tuple[tuple[str, ...], ...] = ()


#: What a publish copies, in load order. Derived from what `serve.queries` and
#: `serve.explain` read, which a test checks against their SQL so a new read
#: cannot quietly target a table the copy lacks.
#:
#: Left out on purpose, because nothing a visitor sees is built from them:
#: the pipeline's bookkeeping (`results_filings`, `filings`, `extraction_*`,
#: `news_backfill_log`), backtest output (`backtest_*`), the resolver's
#: `instrument_aliases`, and the universe tables (`index_membership*`,
#: `corporate_actions`) that only scoring reads. Adding one later is one line.
SITE_TABLES: tuple[SiteTable, ...] = (
    # serve.queries — every API route reads through these.
    SiteTable("instruments"),
    SiteTable("signals", indexes=(("as_of_date",),)),      # signals_on, scored_dates
    SiteTable("factor_scores", indexes=(("as_of_date",),)),  # explain.factor_panel
    SiteTable("news", indexes=(("isin", "published_at"),)),  # recent_news, sentiment_counts
    SiteTable("news_sentiment"),
    SiteTable("fundamentals_annual"),
    SiteTable("fundamentals_quarterly"),
    # No route reads these yet. They are the site's next pages — price charts
    # and the shareholding pattern — and prices are most of the copy's size,
    # so the cost of carrying them is visible from the first publish rather
    # than arriving as a surprise with the route that needs them.
    SiteTable("prices_daily", indexes=(("date",),)),       # PK covers (isin, date)
    SiteTable("shareholding"),
)

# DuckDB column type -> Postgres column type. Anything not listed stops the
# publish before Postgres is touched: guessing a mapping for a type nobody has
# looked at is how a copy ends up subtly different from its source.
_PG_TYPES = {
    "VARCHAR": 'text COLLATE "C"',
    "DOUBLE": "double precision",
    "FLOAT": "real",
    "BIGINT": "bigint",
    "INTEGER": "integer",
    "SMALLINT": "smallint",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamptz",
}

# Rows pulled from DuckDB per round trip. Bounds memory on a whole-market
# prices table without making the per-batch overhead show.
_FETCH_ROWS = 20_000

# Each release records itself in its own schema, so what is live and what can
# be rolled back to is read from Postgres, never remembered locally.
_RELEASE_TABLE = "_release"

Progress = Callable[[int, int, str, int], None]


@dataclass(frozen=True)
class _Column:
    name: str
    duck_type: str
    nullable: bool

    @property
    def pg_type(self) -> str:
        return _PG_TYPES[self.duck_type]


@dataclass(frozen=True)
class _TablePlan:
    table: SiteTable
    columns: tuple[_Column, ...]
    primary_key: tuple[str, ...]


@dataclass(frozen=True)
class Release:
    release_id: str
    schema: str                 # where it physically lives right now
    live: bool
    published_at: dt.datetime | None
    demoted_at: dt.datetime | None
    row_counts: dict[str, int] = field(default_factory=dict)
    source: str | None = None

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


@dataclass(frozen=True)
class PublishResult:
    release_id: str
    schema: str
    row_counts: dict[str, int]
    seconds: float
    previous: str | None = None       # the release this one replaced
    dropped: tuple[str, ...] = ()     # schemas removed by retention
    drop_failures: tuple[str, ...] = ()

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


@dataclass(frozen=True)
class RollbackResult:
    schema: str
    live: str          # release now live
    replaced: str      # release it replaced, kept for rolling forward again


def release_schema(live: str, release_id: str) -> str:
    return f"{live}_release_{release_id}"


# ----------------------------------------------------------------------
# Reading the source
# ----------------------------------------------------------------------


def plan_tables(db: Database, tables: Sequence[SiteTable] = SITE_TABLES) -> list[_TablePlan]:
    """Columns and keys for every table, read from DuckDB before Postgres is touched.

    Read from the catalog rather than declared, so a column the schema gains
    through an `ALTER TABLE` migration is published without anyone remembering
    to list it here.
    """
    plans = []
    for table in tables:
        rows = db.conn.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM duckdb_columns()
            WHERE database_name = current_database()
              AND schema_name = current_schema()
              AND table_name = ?
            ORDER BY column_index
            """,
            [table.name],
        ).fetchall()
        if not rows:
            raise PublishError(f"DuckDB has no table {table.name!r} to publish.")
        columns = tuple(_Column(name, dtype, bool(nullable)) for name, dtype, nullable in rows)
        unsupported = [f"{c.name} {c.duck_type}" for c in columns if c.duck_type not in _PG_TYPES]
        if unsupported:
            raise PublishError(
                f"{table.name}: no Postgres mapping for {', '.join(unsupported)}. "
                f"Add one to _PG_TYPES in db/publish.py."
            )

        pk = db.conn.execute(
            """
            SELECT constraint_column_names
            FROM duckdb_constraints()
            WHERE database_name = current_database()
              AND schema_name = current_schema()
              AND table_name = ?
              AND constraint_type = 'PRIMARY KEY'
            """,
            [table.name],
        ).fetchone()
        plans.append(_TablePlan(table, columns, tuple(pk[0]) if pk else ()))
    return plans


def _duck_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _select_sql(plan: _TablePlan) -> str:
    parts = []
    for col in plan.columns:
        ident = _duck_ident(col.name)
        # Postgres text cannot hold NUL, and one stray byte in a scraped
        # article would otherwise fail every publish until the source row was
        # fixed by hand. It is never meaningful in text, so it is dropped here.
        parts.append(
            f"replace({ident}, chr(0), '') AS {ident}" if col.duck_type == "VARCHAR" else ident
        )
    return f"SELECT {', '.join(parts)} FROM {_duck_ident(plan.table.name)}"


# ----------------------------------------------------------------------
# Publish
# ----------------------------------------------------------------------


def publish(
    db: Database,
    dsn: str | None = None,
    *,
    schema: str | None = None,
    keep: int | None = None,
    tables: Sequence[SiteTable] = SITE_TABLES,
    progress: Progress | None = None,
) -> PublishResult:
    """Copy `tables` from `db` into a new release and make it the live schema.

    `db` only needs to be readable: a read-only open from the CLI, or the
    pipeline's own connection when this runs as the last step of a job.
    `progress(i, n, table, rows)` is called after each table loads, inside the
    transaction, so raising from it — a cancelled job — abandons the publish
    with nothing changed.
    """
    dsn = dsn or configured_dsn()
    psycopg = require_psycopg()
    from psycopg.types.json import Jsonb

    live = schema or settings.pg_schema
    keep = max(0, settings.pg_keep_releases if keep is None else keep)
    started = time.monotonic()

    plans = plan_tables(db, tables)

    with psycopg.connect(dsn, application_name="stockanalysis-publish") as pg:
        _lock(pg, live)
        _foreign_schema_check(pg, live)
        # Server defaults on managed services are sized for web requests; a
        # whole-market COPY is not one. Scoped to this transaction only.
        pg.execute("SET LOCAL statement_timeout = 0")

        release_id = _new_release_id(pg, live)
        target = release_schema(live, release_id)
        _execute(pg, "CREATE SCHEMA {}", target)

        counts = _load(db, pg, target, plans, progress)

        _execute(
            pg,
            "CREATE TABLE {}." + _RELEASE_TABLE + " ("
            " release_id text PRIMARY KEY,"
            " published_at timestamptz NOT NULL,"
            " source text,"
            " row_counts jsonb NOT NULL,"
            " demoted_at timestamptz)",
            target,
        )
        _execute(
            pg,
            "INSERT INTO {}." + _RELEASE_TABLE + " VALUES (%s, clock_timestamp(), %s, %s, NULL)",
            target,
            params=[release_id, db.path, Jsonb(counts)],
        )

        previous = _live_release_id(pg, live)
        if previous is not None:
            _demote(pg, live, previous)
        _execute(pg, "ALTER SCHEMA {} RENAME TO {}", target, live)
        pg.commit()

        # The switch is committed. Everything from here is housekeeping, and a
        # failure in it must not be reported as a failed publish.
        pg.autocommit = True
        dropped, failures = _prune(pg, live, keep)

    return PublishResult(
        release_id=release_id,
        schema=live,
        row_counts=counts,
        seconds=time.monotonic() - started,
        previous=previous,
        dropped=dropped,
        drop_failures=failures,
    )


def _load(
    db: Database,
    pg: Any,
    target: str,
    plans: list[_TablePlan],
    progress: Progress | None,
) -> dict[str, int]:
    """Create, fill, index and verify every table in `target`. Returns row counts.

    The DuckDB reads share one transaction so every table comes from the same
    snapshot, and the counts the copy is checked against are counts of exactly
    what was copied.
    """
    counts: dict[str, int] = {}
    db.conn.execute("BEGIN TRANSACTION")
    try:
        for i, plan in enumerate(plans, start=1):
            name = plan.table.name
            expected = db.conn.execute(
                f"SELECT count(*) FROM {_duck_ident(name)}"
            ).fetchone()[0]

            _create_table(pg, target, plan)
            copied = _copy_rows(db, pg, target, plan)

            # Keys and indexes after the load, not before: building an index
            # once over finished data is far cheaper than maintaining it row by
            # row through a COPY.
            if plan.primary_key:
                _execute(
                    pg, "ALTER TABLE {}.{} ADD PRIMARY KEY ({})",
                    target, name, list(plan.primary_key),
                )
            for columns in plan.table.indexes:
                _execute(pg, "CREATE INDEX ON {}.{} ({})", target, name, list(columns))
            # A fresh table has no statistics, and a planner without them can
            # pick a sequential scan over prices for a one-company lookup.
            _execute(pg, "ANALYZE {}.{}", target, name)

            landed = pg.execute(
                _sql("SELECT count(*) FROM {}.{}", target, name)
            ).fetchone()[0]
            if not expected == copied == landed:
                raise PublishError(
                    f"{name}: DuckDB has {expected:,} rows, {copied:,} were sent "
                    f"and {landed:,} arrived. Nothing was switched."
                )
            counts[name] = landed
            if progress is not None:
                progress(i, len(plans), name, landed)
    finally:
        db.conn.execute("ROLLBACK")
    return counts


def _create_table(pg: Any, target: str, plan: _TablePlan) -> None:
    from psycopg import sql

    columns = sql.SQL(", ").join(
        sql.SQL("{} {}{}").format(
            sql.Identifier(col.name),
            sql.SQL(col.pg_type),
            sql.SQL("" if col.nullable else " NOT NULL"),
        )
        for col in plan.columns
    )
    pg.execute(
        sql.SQL("CREATE TABLE {}.{} ({})").format(
            sql.Identifier(target), sql.Identifier(plan.table.name), columns
        )
    )


def _copy_rows(db: Database, pg: Any, target: str, plan: _TablePlan) -> int:
    """Stream one table from DuckDB into Postgres with COPY. Returns rows sent."""
    from psycopg import sql

    statement = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        sql.Identifier(target),
        sql.Identifier(plan.table.name),
        sql.SQL(", ").join(sql.Identifier(col.name) for col in plan.columns),
    )
    sent = 0
    result = db.conn.execute(_select_sql(plan))
    with pg.cursor() as cur, cur.copy(statement) as copy:
        while rows := result.fetchmany(_FETCH_ROWS):
            for row in rows:
                copy.write_row(row)
            sent += len(rows)
    return sent


def _new_release_id(pg: Any, live: str) -> str:
    """A UTC timestamp, made unique if two publishes land in the same second.

    Checked against the live release's id as well as the kept schemas: the
    live one is named after the schema, not its id, but is renamed back to
    its id the moment it is replaced.
    """
    taken = {r.release_id for r in _releases(pg, live)}
    base = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")
    candidate, n = base, 1
    while candidate in taken or _schema_exists(pg, release_schema(live, candidate)):
        n += 1
        candidate = f"{base}_{n}"
    return candidate


# ----------------------------------------------------------------------
# Rollback and listing
# ----------------------------------------------------------------------


def rollback(dsn: str | None = None, *, schema: str | None = None) -> RollbackResult:
    """Make the most recently replaced release live again, atomically.

    The release being rolled back from is kept, now as the most recently
    replaced one, so a second rollback undoes the first. Going further back is
    a re-publish from DuckDB, which is the source of truth anyway.
    """
    dsn = dsn or configured_dsn()
    psycopg = require_psycopg()
    live = schema or settings.pg_schema

    with psycopg.connect(dsn, application_name="stockanalysis-publish") as pg:
        _lock(pg, live)
        _foreign_schema_check(pg, live)
        current = _live_release_id(pg, live)
        if current is None:
            raise PublishError(f"Nothing is live in {live!r} — publish first.")

        candidates = [r for r in _releases(pg, live) if not r.live]
        if not candidates:
            raise PublishError(
                f"No previous release of {live!r} is kept to roll back to. "
                f"SA_PG_KEEP_RELEASES is {settings.pg_keep_releases}."
            )
        target = candidates[0]

        _demote(pg, live, current)
        _execute(
            pg, "UPDATE {}." + _RELEASE_TABLE + " SET demoted_at = NULL", target.schema
        )
        _execute(pg, "ALTER SCHEMA {} RENAME TO {}", target.schema, live)
        pg.commit()

    return RollbackResult(schema=live, live=target.release_id, replaced=current)


def list_releases(dsn: str | None = None, *, schema: str | None = None) -> list[Release]:
    """The live release first, then the kept ones, most recently replaced first."""
    dsn = dsn or configured_dsn()
    psycopg = require_psycopg()
    live = schema or settings.pg_schema
    with psycopg.connect(dsn, autocommit=True, application_name="stockanalysis-publish") as pg:
        return _releases(pg, live)


def _releases(pg: Any, live: str) -> list[Release]:
    prefix = release_schema(live, "")
    names = [
        row[0]
        for row in pg.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname = %s OR starts_with(nspname, %s)",
            [live, prefix],
        ).fetchall()
    ]
    out = []
    for name in names:
        if not _has_release_table(pg, name):
            continue      # not ours — never listed, so never rolled to or dropped
        release_id, published_at, source, row_counts, demoted_at = pg.execute(
            _sql(
                "SELECT release_id, published_at, source, row_counts, demoted_at "
                "FROM {}." + _RELEASE_TABLE,
                name,
            )
        ).fetchone()
        out.append(
            Release(
                release_id=release_id,
                schema=name,
                live=name == live,
                published_at=published_at,
                demoted_at=demoted_at,
                row_counts=dict(row_counts or {}),
                source=source,
            )
        )

    # Live first; then by when each stopped being live, most recent first. That
    # order — not the release id — is what "previous" means once a rollback has
    # put an older release back in front of a newer one.
    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    out.sort(key=lambda r: (not r.live, -(r.demoted_at or epoch).timestamp()))
    return out


# ----------------------------------------------------------------------
# Shared steps of the switch
# ----------------------------------------------------------------------


def _lock(pg: Any, live: str) -> None:
    """One publish or rollback per live schema at a time, across machines.

    A session-level advisory lock, so it holds through the commit and the
    retention pass after it, and is released when the connection closes —
    including when the process dies. Refused rather than waited for: a second
    publish queued behind a long one would only republish the same data.
    """
    acquired = pg.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s))", [f"stockanalysis.publish:{live}"]
    ).fetchone()[0]
    if not acquired:
        raise PublishBusy(
            f"Another publish or rollback of {live!r} is in progress. Try again "
            f"when it finishes."
        )


def _foreign_schema_check(pg: Any, live: str) -> None:
    """Refuse to replace a live schema this module did not create.

    A publish renames the live schema away and eventually drops it. Pointed by
    mistake at a schema holding anything else, that would destroy it, so a
    schema without a release record is never touched.
    """
    if _schema_exists(pg, live) and not _has_release_table(pg, live):
        raise PublishError(
            f"Schema {live!r} exists but was not created by `stockanalysis "
            f"publish` (it has no {_RELEASE_TABLE} table). Refusing to replace "
            f"it — choose another SA_PG_SCHEMA or remove it yourself."
        )


def _live_release_id(pg: Any, live: str) -> str | None:
    if not _schema_exists(pg, live):
        return None
    return pg.execute(_sql("SELECT release_id FROM {}." + _RELEASE_TABLE, live)).fetchone()[0]


def _demote(pg: Any, live: str, release_id: str) -> None:
    """Move the live release aside under its own name, stamped with when."""
    _execute(
        pg, "UPDATE {}." + _RELEASE_TABLE + " SET demoted_at = clock_timestamp()", live
    )
    _execute(pg, "ALTER SCHEMA {} RENAME TO {}", live, release_schema(live, release_id))


def _prune(pg: Any, live: str, keep: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Drop kept releases beyond the newest `keep`. Runs after the switch commits.

    Best effort, one schema per statement. A DROP waits for any reader still
    holding one of the old tables; the lock timeout bounds that wait, and a
    release left behind is simply dropped by the next publish instead.
    """
    from psycopg import Error

    try:
        pg.execute("SET lock_timeout = '10s'")
        stale = [r.schema for r in _releases(pg, live) if not r.live][keep:]
    except Error as exc:
        log.warning("could not list releases for retention: %s", exc)
        return (), ()
    dropped, failed = [], []
    for name in stale:
        try:
            _execute(pg, "DROP SCHEMA {} CASCADE", name)
            dropped.append(name)
        except Error as exc:
            log.warning("could not drop old release %s: %s", name, exc)
            failed.append(name)
    return tuple(dropped), tuple(failed)


def _schema_exists(pg: Any, name: str) -> bool:
    return pg.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname = %s", [name]
    ).fetchone() is not None


def _has_release_table(pg: Any, schema: str) -> bool:
    return pg.execute(
        "SELECT to_regclass(format('%%I.%%I', %s::text, %s::text)) IS NOT NULL",
        [schema, _RELEASE_TABLE],
    ).fetchone()[0]


def _sql(template: str, *names: str | list[str]) -> Any:
    """`template` with each `{}` filled by a quoted identifier, or a list of them."""
    from psycopg import sql

    parts = [
        sql.SQL(", ").join(sql.Identifier(n) for n in name)
        if isinstance(name, list)
        else sql.Identifier(name)
        for name in names
    ]
    return sql.SQL(template).format(*parts)


def _execute(pg: Any, template: str, *names: str | list[str], params: list | None = None) -> None:
    pg.execute(_sql(template, *names), params)
