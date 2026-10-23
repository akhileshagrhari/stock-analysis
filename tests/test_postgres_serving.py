"""Postgres serving — the publish step, rollback, and API parity with DuckDB.

Two halves. The first runs everywhere: placeholder translation, the "not
configured" paths, and checks that the published table set covers everything
the read layer queries. The second needs a real server and runs only when
SA_TEST_PG_DSN names one:

    SA_TEST_PG_DSN=postgresql:///stockanalysis_test .venv/bin/python -m pytest \\
        tests/test_postgres_serving.py

A separate variable from SA_PG_DSN on purpose. These tests create and drop
schemas, and the database a developer has configured for serving is the last
place that should happen. Each test works in a schema named for it alone and
drops every release of it afterwards.

The parity tests are the point of the second half: the same fixture database
served from DuckDB and from its published Postgres copy must produce
byte-identical responses on every route. The fixture carries the cases that
differ between the engines by default — NaN beside NULL, a symbol with
punctuation (`M&M`) tied on score with one without, an empty sector, Unicode,
a NULL boolean — because a parity test over tidy data proves nothing.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pandas as pd
import pytest
from conftest import (
    DEFAULT_MOMENTUM_STRENGTH,
    make_instruments,
    make_membership,
    make_prices,
    make_shareholding,
)
from pydantic import SecretStr

from stockanalysis.config import Settings, settings
from stockanalysis.db import publish as pub
from stockanalysis.db.database import Database
from stockanalysis.db.postgres import PostgresUnavailable, to_pyformat
from stockanalysis.run.events import JobState, StepState
from stockanalysis.run.runner import execute_plan
from stockanalysis.run.steps import FREE, STEPS, STEPS_BY_KEY, Plan, RunOptions

TEST_DSN = os.environ.get("SA_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(
    not TEST_DSN, reason="SA_TEST_PG_DSN not set — Postgres integration tests skipped"
)

INDEX = "TESTIDX"
AS_OF = dt.date(2023, 1, 31)
EARLIER = dt.date(2022, 12, 30)

FIRST = "INE000000000"         # a scored TESTIDX company with news and a red flag
MM = "INE00MM00001"            # "M&M" — punctuation in the symbol, tied with MARICO
MARICO = "INE00MAR0001"
BAJAJ = "INE00BAJ0001"         # lowercase symbol, Unicode name, empty sector, NaN score
NOSIGNAL = "INE00NUL0001"      # instrument with no signal at all


# ----------------------------------------------------------------------
# Runs everywhere
# ----------------------------------------------------------------------


class TestPlaceholders:
    @pytest.mark.parametrize(
        ("duck", "pg"),
        [
            ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = %s AND b = %s"),
            ("SELECT * FROM t WHERE a IN (?, ?)", "SELECT * FROM t WHERE a IN (%s, %s)"),
            # A literal `?` is data, not a placeholder.
            ("SELECT '?' AS q, a FROM t WHERE b = ?", "SELECT '?' AS q, a FROM t WHERE b = %s"),
            # psycopg reads `%` everywhere, quoted or not, so every one doubles.
            ("SELECT a % 2, '50%' FROM t WHERE b = ?", "SELECT a %% 2, '50%%' FROM t WHERE b = %s"),
            # Doubled quotes inside a literal do not end it early.
            ("SELECT 'it''s ?' FROM t WHERE b = ?", "SELECT 'it''s ?' FROM t WHERE b = %s"),
            ('SELECT "odd?col" FROM t WHERE b = ?', 'SELECT "odd?col" FROM t WHERE b = %s'),
            ("SELECT a -- why?\nFROM t WHERE b = ?", "SELECT a -- why?\nFROM t WHERE b = %s"),
            ("SELECT /* what? */ a FROM t WHERE b = ?", "SELECT /* what? */ a FROM t WHERE b = %s"),
        ],
    )
    def test_translation(self, duck, pg):
        assert to_pyformat(duck) == pg


class TestSiteTables:
    def test_every_table_the_read_layer_queries_is_published(self):
        """The publish set is derived from the read layer's SQL, and stays so.

        A route that starts reading a new table must fail here, in the default
        suite, rather than as a 503 on the Postgres backend in production.
        """
        import stockanalysis.serve.api as api_mod
        import stockanalysis.serve.explain as explain_mod
        import stockanalysis.serve.queries as queries_mod

        referenced: set[str] = set()
        for module in (queries_mod, explain_mod, api_mod):
            source = Path(module.__file__).read_text()
            # Upper-case keywords only: SQL here is written that way and prose
            # is not, so "from the" in a docstring is not mistaken for a table.
            referenced |= set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)\b", source))

        published = {t.name for t in pub.SITE_TABLES}
        assert referenced, "the scan found no tables — the regex is broken"
        assert referenced <= published, (
            f"read by serve/ but not published: {sorted(referenced - published)}. "
            f"Add them to SITE_TABLES in db/publish.py."
        )

    def test_bookkeeping_is_not_published(self):
        names = {t.name for t in pub.SITE_TABLES}
        for table in (
            "results_filings", "filings", "extraction_attempts", "extraction_review",
            "news_backfill_log", "backtest_runs", "backtest_positions", "backtest_nav",
        ):
            assert table not in names

    def test_every_site_table_maps_to_postgres(self, db):
        """Read from the live schema, so a migration adding an unmapped type
        fails here rather than halfway through somebody's publish."""
        plans = {p.table.name: p for p in pub.plan_tables(db)}
        assert set(plans) == {t.name for t in pub.SITE_TABLES}
        assert plans["prices_daily"].primary_key == ("isin", "date")
        assert all(p.primary_key for p in plans.values())
        text = [c for p in plans.values() for c in p.columns if c.duck_type == "VARCHAR"]
        assert text and all('COLLATE "C"' in c.pg_type for c in text)

    def test_unmapped_type_stops_before_postgres_is_touched(self, db):
        db.conn.execute("CREATE TABLE oddities (id INTEGER PRIMARY KEY, tags VARCHAR[])")
        with pytest.raises(pub.PublishError, match=r"tags VARCHAR\[\]"):
            pub.plan_tables(db, [pub.SiteTable("oddities")])


class TestUnconfigured:
    def test_defaults_leave_everything_on_duckdb(self, monkeypatch):
        for var in ("SA_PG_DSN", "SA_SERVE_BACKEND"):
            monkeypatch.delenv(var, raising=False)
        fresh = Settings(_env_file=None)
        assert fresh.pg_dsn is None
        assert fresh.serve_backend == "duckdb"

    @pytest.mark.parametrize("name", ["public", "pg_temp", "Site", "1site", ""])
    def test_schema_setting_refuses_shared_or_odd_names(self, name):
        with pytest.raises(ValueError):
            Settings(_env_file=None, pg_schema=name)

    def test_publish_says_what_to_configure(self, db):
        with pytest.raises(PostgresUnavailable, match="SA_PG_DSN"):
            pub.publish(db)

    def test_publish_is_the_last_step_and_runs_by_default(self):
        spec = STEPS[-1]
        assert spec.key == "publish"
        assert spec.default_on and spec.cost == FREE
        assert set(spec.scopes) == {"company", "universe"}

    def test_publish_step_skips_with_the_reason(self, db):
        job = execute_plan(db, _publish_plan())
        assert job.state == JobState.DONE
        record = job.step("publish")
        assert record.state == StepState.SKIPPED
        assert "SA_PG_DSN" in record.message and "DuckDB" in record.message

    def test_api_on_postgres_without_dsn_is_503(self, monkeypatch):
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from stockanalysis.serve.api import app

        monkeypatch.setattr(settings, "serve_backend", "postgres")
        response = fastapi_testclient.TestClient(app).get("/health")
        assert response.status_code == 503
        assert "SA_PG_DSN" in response.json()["detail"]

    def test_cli_publish_without_dsn_fails_readably(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from stockanalysis.cli import app

        path = tmp_path / "empty.duckdb"
        Database(path).close()
        monkeypatch.setattr(settings, "db_path", path)
        result = CliRunner().invoke(app, ["publish"])
        assert result.exit_code == 1
        assert "SA_PG_DSN" in result.output


# ----------------------------------------------------------------------
# Fixtures for the Postgres half
# ----------------------------------------------------------------------


def _publish_plan() -> Plan:
    return Plan(
        title="publish",
        scope="universe",
        steps=(STEPS_BY_KEY["publish"],),
        options=RunOptions(index_name=INDEX, as_of=AS_OF),
    )


def _edge_cases(db: Database) -> None:
    """The rows on which DuckDB and Postgres disagree unless something handles it."""
    db.upsert_df(
        "instruments",
        pd.DataFrame(
            [
                (MM, "M&M", "500520", "Mahindra & Mahindra", "Metals & Mining", None, None),
                (MARICO, "MARICO", None, "Marico", "Metals & Mining", None, dt.date(2010, 1, 1)),
                (BAJAJ, "bajaj-auto", None, "Bajaj Ünïcødé ₹ Ltd", "", None, None),
                (NOSIGNAL, "NOSIGNAL", None, "No Signal Co", None, None, None),
            ],
            columns=[
                "isin", "nse_symbol", "bse_code", "name", "sector", "industry", "listing_date",
            ],
        ),
        ["isin"],
    )

    nan, inf = float("nan"), float("inf")
    db.upsert_df(
        "signals",
        pd.DataFrame(
            [
                # M&M and MARICO tie on score, so their order rests on the
                # symbol — byte order in DuckDB, locale order in a default
                # Postgres database, unless the column collation is pinned.
                (MM, AS_OF, 55.0, "HOLD", "", "pledge, rating", 0.6,
                 'Ties with "MARICO" at 55% — why? It\'s a tie.', "test"),
                (MARICO, AS_OF, 55.0, "HOLD", None, None, 0.6, None, "test"),
                (BAJAJ, AS_OF, nan, None, None, None, inf, "Ünïcødé ₹", None),
                (MM, EARLIER, 40.0, "SELL", "qualified_audit", "", 0.5, None, "test"),
            ],
            columns=[
                "isin", "as_of_date", "composite_score", "signal", "red_flags",
                "unknown_flags", "coverage", "narrative", "model_version",
            ],
        ),
        ["isin", "as_of_date"],
    )
    db.upsert_df(
        "factor_scores",
        pd.DataFrame(
            [
                (MM, AS_OF, "roe", 0.21, 1.25),
                (MM, AS_OF, "debt_to_equity", nan, None),
                (MM, AS_OF, "pat_cagr_3y", None, -0.5),
            ],
            columns=["isin", "as_of_date", "factor_name", "raw_value", "sector_zscore"],
        ),
        ["isin", "as_of_date", "factor_name"],
    )
    db.upsert_df(
        "news",
        pd.DataFrame(
            [
                ("mm-1", MM, dt.datetime(2023, 1, 30, 9, 30, 0, 123456), "M&M up 5% — why?",
                 None, None),
                ("mm-2", MM, dt.datetime(2023, 1, 29, 18, 0), "Ünïcødé headline", "RSS",
                 "https://example.test/mm-2"),
                ("orphan", None, dt.datetime(2023, 1, 30, 8, 0), "Nobody's news", "RSS", None),
            ],
            columns=["news_id", "isin", "published_at", "headline", "source", "url"],
        ),
        ["news_id"],
    )
    db.upsert_df(
        "news_sentiment",
        pd.DataFrame(
            [("mm-1", "test-model", "positive", nan, None)],
            columns=["news_id", "model", "label", "score", "computed_at"],
        ),
        ["news_id", "model"],
    )
    db.upsert_df(
        "fundamentals_quarterly",
        pd.DataFrame(
            [
                (FIRST, dt.date(2022, 6, 30), dt.date(2022, 8, 10), 250.0, 40.0, 4.0, True),
                (FIRST, dt.date(2022, 9, 30), dt.date(2022, 11, 10), 260.0, nan, 4.2, False),
                (FIRST, dt.date(2022, 12, 31), dt.date(2023, 2, 10), None, 45.0, None, None),
            ],
            columns=[
                "isin", "period_end_date", "filing_date", "revenue", "pat", "eps",
                "is_consolidated",
            ],
        ),
        ["isin", "period_end_date"],
    )
    db.upsert_df(
        "shareholding",
        make_shareholding(
            FIRST, [dt.date(2022, 6, 30), dt.date(2022, 9, 30)], [50.5, 50.1]
        ),
        ["isin", "quarter_end"],
    )
    # NaN beside NULL again, and a NULL in the one integer column a route
    # returns: psycopg hands back None in an object column, DuckDB a float NaN.
    db.upsert_df(
        "prices_daily",
        pd.DataFrame(
            [
                (MM, dt.date(2023, 1, 27), nan, None, 99.0, 100.5, None, None, None),
                (MM, dt.date(2023, 1, 30), 100.0, 102.0, 98.5, 101.25, 101.0, 12345, 1.2e6),
            ],
            columns=[
                "isin", "date", "open", "high", "low", "close", "adj_close", "volume",
                "traded_value",
            ],
        ),
        ["isin", "date"],
    )


def build_site_db(path: Path) -> None:
    """The scored phase-4 fixture plus the edge cases, as a file on disk."""
    from test_phase4_serving import _seed_scored

    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)
    db = Database(path)
    instruments = make_instruments(30)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    db.upsert_df(
        "prices_daily",
        make_prices(isins, start, end, momentum_strength=DEFAULT_MOMENTUM_STRENGTH),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, INDEX, start),
        ["index_name", "isin", "from_date"],
    )
    _seed_scored(db)
    _edge_cases(db)
    db.close()


@pytest.fixture
def pg_schema():
    """A live-schema name for this test alone; every release of it dropped after."""
    if not TEST_DSN:
        pytest.skip("SA_TEST_PG_DSN not set")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    from stockanalysis.db.postgres import close_reader_pools

    name = f"sa_test_{uuid.uuid4().hex[:10]}"
    yield name
    close_reader_pools()
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        for (schema,) in pg.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname = %s OR starts_with(nspname, %s)",
            [name, name + "_"],
        ).fetchall():
            pg.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def site(tmp_path, monkeypatch, pg_schema):
    """The fixture database on disk, with settings pointed at it and at Postgres.

    Nothing is published yet: each test decides when, so the "before the
    first publish" state is reachable too.
    """
    path = tmp_path / "site.duckdb"
    build_site_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(settings, "pg_dsn", SecretStr(TEST_DSN))
    monkeypatch.setattr(settings, "pg_schema", pg_schema)
    monkeypatch.setattr(settings, "pg_keep_releases", 1)
    return SimpleNamespace(path=path, schema=pg_schema)


@pytest.fixture
def published(site):
    _publish(site)
    return site


def _publish(site, **kwargs) -> pub.PublishResult:
    with Database(site.path, read_only=True) as db:
        return pub.publish(db, TEST_DSN, schema=site.schema, **kwargs)


def _add_instrument(site, isin: str, symbol: str) -> None:
    with Database(site.path) as db:
        db.upsert_df(
            "instruments",
            pd.DataFrame([{"isin": isin, "nse_symbol": symbol, "name": symbol}]),
            ["isin"],
        )


def _pg_scalar(query: str, *names: str):
    import psycopg
    from psycopg import sql

    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        statement = sql.SQL(query).format(*(sql.Identifier(n) for n in names))
        return pg.execute(statement).fetchone()[0]


def _live_instruments(site) -> int:
    return _pg_scalar("SELECT count(*) FROM {}.instruments", site.schema)


def _schemas(site) -> set[str]:
    import psycopg

    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        return {
            row[0]
            for row in pg.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = %s OR starts_with(nspname, %s)",
                [site.schema, site.schema + "_"],
            ).fetchall()
        }


# ----------------------------------------------------------------------
# Publish and rollback
# ----------------------------------------------------------------------


@needs_pg
class TestPublish:
    def test_copies_every_site_table_with_matching_counts(self, site):
        result = _publish(site)

        with Database(site.path, read_only=True) as db:
            for table in pub.SITE_TABLES:
                expected = db.query(f"SELECT count(*) AS n FROM {table.name}")["n"][0]
                landed = _pg_scalar(f"SELECT count(*) FROM {{}}.{table.name}", site.schema)
                assert landed == expected == result.row_counts[table.name], table.name
        assert result.previous is None
        assert _schemas(site) == {site.schema}

    def test_keys_indexes_and_collation(self, published):
        import psycopg

        with psycopg.connect(TEST_DSN, autocommit=True) as pg:
            indexes = {
                (row[0], row[1])
                for row in pg.execute(
                    "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s",
                    [published.schema],
                ).fetchall()
            }
            collations = {
                row[0]
                for row in pg.execute(
                    "SELECT DISTINCT collation_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND data_type = 'text' "
                    "AND table_name <> '_release'",
                    [published.schema],
                ).fetchall()
            }
        defs = " | ".join(d for _t, d in indexes)
        assert "prices_daily_pkey ON" in defs and "(isin, date)" in defs
        assert any(t == "signals" and "(as_of_date)" in d for t, d in indexes)
        assert any(t == "news" and "(isin, published_at)" in d for t, d in indexes)
        assert collations == {"C"}

    def test_republish_switches_and_keeps_the_previous_release(self, published):
        before = _live_instruments(published)
        first = pub.list_releases(TEST_DSN, schema=published.schema)[0]

        _add_instrument(published, "INE00NEW0001", "NEWCO")
        result = _publish(published)

        assert _live_instruments(published) == before + 1
        assert result.previous == first.release_id
        releases = pub.list_releases(TEST_DSN, schema=published.schema)
        assert [(r.release_id, r.live) for r in releases] == [
            (result.release_id, True),
            (first.release_id, False),
        ]
        assert releases[1].schema == pub.release_schema(published.schema, first.release_id)

    def test_retention_drops_all_but_the_newest_kept(self, published):
        _publish(published)
        third = _publish(published)
        assert len(third.dropped) == 1
        assert len(_schemas(published)) == 2      # live + one kept

        _publish(published, keep=0)
        assert _schemas(published) == {published.schema}

    def test_rollback_restores_the_previous_release_and_can_be_undone(self, published):
        old = _live_instruments(published)
        _add_instrument(published, "INE00NEW0001", "NEWCO")
        new_release = _publish(published)
        assert _live_instruments(published) == old + 1

        back = pub.rollback(TEST_DSN, schema=published.schema)
        assert back.replaced == new_release.release_id
        assert _live_instruments(published) == old

        forward = pub.rollback(TEST_DSN, schema=published.schema)
        assert forward.live == new_release.release_id
        assert _live_instruments(published) == old + 1

    def test_publish_after_rollback_keeps_the_release_that_was_live(self, published):
        """Retention follows when a release stopped being live, not its id —
        otherwise the bad release rolled away from would outlive the good one."""
        good = pub.list_releases(TEST_DSN, schema=published.schema)[0]
        _publish(published)                                   # the "bad" one
        pub.rollback(TEST_DSN, schema=published.schema)       # good is live again
        result = _publish(published)

        assert result.previous == good.release_id
        kept = [r for r in pub.list_releases(TEST_DSN, schema=published.schema) if not r.live]
        assert [r.release_id for r in kept] == [good.release_id]

    def test_rollback_with_nothing_kept_refuses(self, published):
        with pytest.raises(pub.PublishError, match="No previous release"):
            pub.rollback(TEST_DSN, schema=published.schema)
        assert _schemas(published) == {published.schema}

    def test_rollback_before_any_publish_refuses(self, site):
        with pytest.raises(pub.PublishError, match="publish first"):
            pub.rollback(TEST_DSN, schema=site.schema)


@needs_pg
class TestFailedPublishChangesNothing:
    def _state(self, site):
        releases = pub.list_releases(TEST_DSN, schema=site.schema)
        return (
            _schemas(site),
            [(r.release_id, r.live) for r in releases],
            _live_instruments(site),
        )

    def test_failure_mid_load(self, published):
        before = self._state(published)
        _add_instrument(published, "INE00NEW0001", "NEWCO")

        def explode(i, n, table, rows):
            if i == 4:
                raise RuntimeError("simulated crash after four tables")

        with pytest.raises(RuntimeError, match="simulated crash"):
            _publish(published, progress=explode)
        assert self._state(published) == before

    def test_row_count_mismatch_aborts_the_switch(self, published, monkeypatch):
        before = self._state(published)
        original = pub._select_sql

        def lossy(plan):
            sql = original(plan)
            return sql + " LIMIT 5" if plan.table.name == "prices_daily" else sql

        monkeypatch.setattr(pub, "_select_sql", lossy)
        with pytest.raises(pub.PublishError, match="prices_daily"):
            _publish(published)
        assert self._state(published) == before

    def test_readers_see_the_old_release_until_the_switch(self, published):
        """Mid-publish, the live schema still answers in full — the new release
        is invisible until COMMIT, and never visible half-loaded."""
        from stockanalysis.db.postgres import pooled_reader

        old = _live_instruments(published)
        _add_instrument(published, "INE00NEW0001", "NEWCO")
        seen: list[int] = []

        def peek(i, n, table, rows):
            with pooled_reader() as reader:
                seen.append(int(reader.query("SELECT count(*) AS n FROM instruments")["n"][0]))
                seen.append(int(reader.query("SELECT count(*) AS n FROM prices_daily")["n"][0]))

        _publish(published, progress=peek)
        assert seen[0::2] == [old] * len(pub.SITE_TABLES)
        assert all(n > 0 for n in seen[1::2])
        assert _live_instruments(published) == old + 1

    def test_refuses_to_replace_a_schema_it_did_not_create(self, site):
        import psycopg
        from psycopg import sql

        with psycopg.connect(TEST_DSN, autocommit=True) as pg:
            pg.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(site.schema)))
            pg.execute(
                sql.SQL("CREATE TABLE {}.precious (x int)").format(sql.Identifier(site.schema))
            )
        with pytest.raises(pub.PublishError, match="not created by"):
            _publish(site)
        assert _pg_scalar("SELECT count(*) FROM {}.precious", site.schema) == 0
        assert _schemas(site) == {site.schema}

    def test_concurrent_publish_is_refused(self, published):
        import psycopg

        with psycopg.connect(TEST_DSN, autocommit=True) as holder:
            holder.execute(
                "SELECT pg_advisory_lock(hashtext(%s))",
                [f"stockanalysis.publish:{published.schema}"],
            )
            with pytest.raises(pub.PublishBusy):
                _publish(published)


@needs_pg
class TestReaderAcrossSwaps:
    def test_a_pooled_connection_reads_the_new_release_without_reconnecting(self, published):
        from stockanalysis.db.postgres import PostgresReader, reader_pool

        pool = reader_pool()
        conn = pool.getconn()
        try:
            reader = PostgresReader(conn)
            before = int(reader.query("SELECT count(*) AS n FROM instruments")["n"][0])
            _add_instrument(published, "INE00NEW0001", "NEWCO")
            _publish(published)
            after = int(reader.query("SELECT count(*) AS n FROM instruments")["n"][0])
        finally:
            pool.putconn(conn)
        assert after == before + 1

    def test_float_and_numeric_columns_come_back_like_duckdb(self, published):
        """An all-NULL float column is float64/NaN from DuckDB; psycopg would
        hand back `object`/None, which breaks arithmetic on a pivot of it. And
        NUMERIC would arrive as Decimal where DuckDB gives a float."""
        from stockanalysis.db.postgres import pooled_reader

        statement = (
            "SELECT CAST(NULL AS DOUBLE PRECISION) AS empty, "
            "CAST(1.5 AS NUMERIC) AS exact, sum(composite_score) AS total "
            "FROM signals WHERE as_of_date = ?"
        )
        with Database(published.path, read_only=True) as duck, pooled_reader() as pg:
            want = duck.query(statement.replace("DOUBLE PRECISION", "DOUBLE"), [AS_OF])
            got = pg.query(statement, [AS_OF])
        assert got["empty"].dtype == want["empty"].dtype == "float64"
        assert got["empty"].isna().all()
        assert type(got["exact"][0]) is type(want["exact"][0])
        assert got["total"].dtype == "float64"

    def test_reader_connections_are_read_only(self, published):
        import psycopg

        from stockanalysis.db.postgres import pooled_reader

        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with pooled_reader() as reader:
                reader.query("DELETE FROM instruments")

    def test_api_before_first_publish_is_503(self, site, monkeypatch):
        from fastapi.testclient import TestClient

        from stockanalysis.serve.api import app

        monkeypatch.setattr(settings, "serve_backend", "postgres")
        response = TestClient(app).get("/health")
        assert response.status_code == 503
        assert "stockanalysis publish" in response.json()["detail"]


@needs_pg
class TestPipelineAndCli:
    def test_publish_runs_as_a_pipeline_step(self, site):
        with Database(site.path) as db:      # writable, as the runner holds it
            job = execute_plan(db, _publish_plan())
        assert job.state == JobState.DONE, job.error
        record = job.step("publish")
        assert record.state == StepState.DONE
        assert record.summary["release"]
        assert [row["table"] for row in record.rows] == [t.name for t in pub.SITE_TABLES]
        assert _live_instruments(site) > 0

    def test_cli_publish_list_and_rollback(self, site):
        from typer.testing import CliRunner

        from stockanalysis.cli import app

        runner = CliRunner()
        first = runner.invoke(app, ["publish"])
        assert first.exit_code == 0, first.output
        assert "Published" in first.output and "prices_daily" in first.output

        second = runner.invoke(app, ["publish"])
        assert second.exit_code == 0, second.output
        assert "kept for `--rollback`" in second.output

        listing = runner.invoke(app, ["publish", "--list"])
        assert listing.exit_code == 0 and "live" in listing.output and "kept" in listing.output

        back = runner.invoke(app, ["publish", "--rollback"])
        assert back.exit_code == 0, back.output
        assert "Rolled back" in back.output


# ----------------------------------------------------------------------
# Parity — the same answers from both backends
# ----------------------------------------------------------------------

PARITY_URLS = [
    "/health",
    "/model",
    "/sectors",
    "/instruments",
    "/instruments?sector=IT",
    "/instruments?sector=Metals%20%26%20Mining",
    "/instruments?sector=No%20such%20sector",
    "/instruments/by-symbol/TEST001",
    "/instruments/by-symbol/m%26m",
    "/instruments/by-symbol/BAJAJ-AUTO",
    "/instruments/by-symbol/NOPE",
    f"/instruments/{FIRST}",
    f"/instruments/{BAJAJ}",
    "/instruments/INE_MISSING",
    f"/instruments/{FIRST}/latest",
    f"/instruments/{MM}/latest",
    f"/instruments/{BAJAJ}/latest",
    f"/instruments/{NOSIGNAL}/latest",
    "/instruments/INE_MISSING/latest",
    f"/instruments/{FIRST}/history",
    f"/instruments/{MM}/history",
    f"/instruments/{MM}/history?limit=1",
    f"/instruments/{NOSIGNAL}/history",
    "/instruments/INE_MISSING/history",
    f"/instruments/{FIRST}/news",
    f"/instruments/{MM}/news?limit=100",
    f"/instruments/{NOSIGNAL}/news",
    "/signals",
    "/signals?limit=1000",
    "/signals?signal=buy",
    "/signals?signal=HOLD&sector=Metals%20%26%20Mining",
    "/signals?signal=SELL&sector=IT",
    f"/signals?as_of={AS_OF}",
    f"/signals?as_of={EARLIER}",
    "/signals?as_of=1999-01-01",
    "/signals?as_of=not-a-date",
    "/signals?signal=MAYBE",
    "/signals?limit=0",
    "/signals/red-flags",
    f"/signals/red-flags?as_of={AS_OF}&limit=1",
    # The public site's routes, at the address it calls them by.
    "/v1/health",
    "/v1/signals",
    f"/v1/instruments/{FIRST}/financials",
    f"/v1/instruments/{FIRST}/financials?quarters=1",
    f"/v1/instruments/{NOSIGNAL}/financials",
    "/v1/instruments/INE_MISSING/financials",
    f"/v1/instruments/{FIRST}/explanation",
    f"/v1/instruments/{MM}/explanation",
    f"/v1/instruments/{BAJAJ}/explanation",
    f"/v1/instruments/{NOSIGNAL}/explanation",
    "/v1/instruments/INE_MISSING/explanation",
    f"/v1/instruments/{FIRST}/prices",
    f"/v1/instruments/{FIRST}/prices?from=2023-06-01&to=2023-06-30",
    f"/v1/instruments/{FIRST}/prices?from=2023-07-01&to=2023-06-01",
    f"/v1/instruments/{FIRST}/prices?from=not-a-date",
    f"/v1/instruments/{MM}/prices",
    f"/v1/instruments/{NOSIGNAL}/prices",
    "/v1/instruments/INE_MISSING/prices",
    "/v1/instruments/search?q=m%26m",
    "/v1/instruments/search?q=test00&limit=3",
    "/v1/instruments/search?q=%C3%9CN%C3%8F",       # ÜNÏ — folded in Python, not SQL
    "/v1/instruments/search?q=%25",
    "/v1/instruments/search?q=",
]


def test_parity_urls_cover_every_route():
    """A route missing from PARITY_URLS has never been checked on Postgres.

    Runs without a server on purpose: adding a route should mean adding it
    here, whether or not the machine adding it has Postgres to hand.
    """
    from fastapi.routing import APIRoute

    from stockanalysis.serve.api import app

    def covers(template: str, url: str) -> bool:
        # `/v1/x` and `/x` are one handler mounted twice (see `serve.api`), so
        # a URL for either covers both.
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", template.removeprefix("/v1"))
        return re.fullmatch(pattern, urlsplit(url).path.removeprefix("/v1")) is not None

    templates = {r.path for r in app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
    uncovered = sorted(t for t in templates if not any(covers(t, u) for u in PARITY_URLS))
    assert not uncovered, (
        f"no parity URL exercises {uncovered}; add one to PARITY_URLS in "
        f"tests/test_postgres_serving.py"
    )


@pytest.fixture
def both_backends(published, monkeypatch):
    from fastapi.testclient import TestClient

    from stockanalysis.serve.api import app

    client = TestClient(app)

    def get(url: str, backend: str):
        monkeypatch.setattr(settings, "serve_backend", backend)
        return client.get(url)

    return get


@needs_pg
@pytest.mark.parametrize("url", PARITY_URLS)
def test_route_returns_identical_json_from_both_backends(both_backends, url):
    duck = both_backends(url, "duckdb")
    pg = both_backends(url, "postgres")
    assert duck.status_code != 503, duck.text
    assert (pg.status_code, pg.content) == (duck.status_code, duck.content)


@needs_pg
def test_parity_fixture_is_not_vacuous(both_backends):
    """The edge cases have to reach the responses, or parity over them is free."""
    body = both_backends("/signals", "postgres").json()
    symbols = [row["nse_symbol"] for row in body]
    assert symbols.index("M&M") + 1 == symbols.index("MARICO")
    bajaj = next(row for row in body if row["nse_symbol"] == "bajaj-auto")
    assert bajaj["composite_score"] is None and bajaj["coverage"] is None
    assert any(row["red_flags"] for row in body)
    assert both_backends("/instruments/by-symbol/m%26m", "postgres").status_code == 200
    news = both_backends(f"/instruments/{MM}/news?limit=100", "postgres").json()
    assert {n["label"] for n in news} == {"positive", None}
    bars = both_backends(f"/v1/instruments/{MM}/prices", "postgres").json()
    assert [bar["volume"] for bar in bars] == [None, 12345]
    assert bars[0]["open"] is None and bars[0]["high"] is None
    found = both_backends("/v1/instruments/search?q=%C3%9CN%C3%8F", "postgres").json()
    assert [row["nse_symbol"] for row in found] == ["bajaj-auto"]


@needs_pg
def test_read_layer_gives_identical_results_on_both_backends(published):
    """Beyond the routes: every read the dashboard and narrative use, and the
    explanation rebuilt from the factor panel, which does arithmetic on the
    frames rather than reading values one at a time."""
    from stockanalysis.db.postgres import pooled_reader
    from stockanalysis.serve import explain, queries

    calls = [
        (queries.latest_as_of, ()),
        (queries.scored_dates, ()),
        (queries.sectors, ()),
        (queries.list_instruments, ()),
        (queries.list_instruments, ("Metals & Mining",)),
        (queries.get_instrument, (BAJAJ,)),
        (queries.resolve_symbol, ("m&m",)),
        (queries.signals_on, ()),
        (queries.signals_on, (AS_OF, None, None, True)),
        (queries.latest_signal, (MM,)),
        (queries.signal_history, (MM,)),
        (queries.factor_breakdown, (MM, AS_OF)),
        (queries.recent_news, (MM, 100)),
        (queries.annual_financials, (FIRST,)),
        (queries.quarterly_financials, (FIRST,)),
        (queries.sentiment_counts, ([FIRST, MM, NOSIGNAL], AS_OF)),
        (queries.signal_counts, (AS_OF,)),
        (queries.latest_price_date, (FIRST,)),
        (queries.latest_price_date, (NOSIGNAL,)),
        (queries.price_history, (MM, dt.date(2023, 1, 1), dt.date(2023, 12, 31))),
        (queries.price_history, (FIRST, dt.date(2023, 6, 1), dt.date(2023, 6, 30))),
        (queries.search_instruments, ("m&m",)),
        (queries.search_instruments, ("üNï", 50)),
        (explain.raw_values, (MM, AS_OF)),
        (explain.dominant_families, (AS_OF,)),
        (explain.explain, (FIRST,)),
        (explain.explain, (MM,)),
    ]
    with Database(published.path, read_only=True) as duck, pooled_reader() as pg:
        for fn, args in calls:
            assert fn(duck, *args) == fn(pg, *args), f"{fn.__name__}{args}"
        pd.testing.assert_frame_equal(
            explain.factor_panel(duck, AS_OF), explain.factor_panel(pg, AS_OF)
        )
        quarters = queries.quarterly_financials(pg, FIRST)
        assert [q.is_consolidated for q in quarters] == [None, False, True]
