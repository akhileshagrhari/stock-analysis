"""The holdings store — needs SA_TEST_PG_DSN, like the Postgres serving tests."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal

import pytest
from pydantic import SecretStr

from stockanalysis.config import settings
from stockanalysis.portfolio import store
from stockanalysis.portfolio.kite import ImportRefused, ParsedRow

TEST_DSN = os.environ.get("SA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="SA_TEST_PG_DSN not set — Postgres integration tests skipped"
)

T1 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
T2 = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
INFY = "INE009A01021"
TCS = "INE467B01029"


def row(sym, q, c, p=None):
    return ParsedRow(sym, Decimal(str(q)), Decimal(str(c)), None if p is None else Decimal(str(p)))


@pytest.fixture
def app(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    from stockanalysis.db.postgres import app_connection, close_reader_pools

    name = f"apptest_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "pg_dsn", SecretStr(TEST_DSN))
    monkeypatch.setattr(settings, "pg_app_schema", name)
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        pg.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        store.ensure_schema(pg)
    yield app_connection
    close_reader_pools()
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        pg.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def save(app, user, rows, isins, mode, now=T1, filename="h.csv"):
    with app() as conn:
        return store.save_import(conn, user, rows, isins, mode, filename, now)


def load(app, user):
    with app() as conn:
        return store.load_holdings(conn, user)


def test_ensure_schema_is_idempotent(app):
    with app() as conn:
        store.ensure_schema(conn)
        store.ensure_schema(conn)


def test_replace_swaps_everything(app):
    save(app, "u", [row("INFY", 1, 1), row("TCS", 2, 2)], {"INFY": INFY, "TCS": TCS}, "replace")
    rec = save(app, "u", [row("ITC", 3, 3)], {}, "replace", T2)
    got = load(app, "u")
    assert [h.symbol for h in got] == ["ITC"]
    assert got[0].isin is None
    assert (rec.rows, rec.matched, rec.mode) == (1, 0, "replace")


def test_add_combines_same_isin(app):
    save(app, "u", [row("INFY", 10, 100, 1000)], {"INFY": INFY}, "replace")
    save(app, "u", [row("INFY", 30, 200, 1500)], {"INFY": INFY}, "add", T2)
    (h,) = load(app, "u")
    assert (h.quantity, h.avg_cost, h.file_price) == (40, 175, 1500)
    assert h.isin == INFY
    assert h.uploaded_at == T2


def test_add_folds_an_unmatched_row_once_it_matches(app):
    save(app, "u", [row("NEWCO", 10, 100)], {}, "replace")
    assert load(app, "u")[0].isin is None
    save(app, "u", [row("NEWCO", 10, 200)], {"NEWCO": "INE000NEW001"}, "add", T2)
    (h,) = load(app, "u")
    assert h.isin == "INE000NEW001"
    assert (h.quantity, h.avg_cost) == (Decimal("20"), Decimal("150"))
    with app() as conn:
        keys = conn.execute(f'SELECT key FROM "{settings.pg_app_schema}".holdings').fetchall()
    assert keys == [("INE000NEW001",)]


def test_add_overflow_is_refused_and_leaves_the_portfolio(app):
    big = Decimal("9999999999999999")
    save(app, "u", [ParsedRow("INFY", big, Decimal(1), None)], {}, "replace")
    with pytest.raises(ImportRefused, match="INFY adds up to too large a holding"):
        save(app, "u", [ParsedRow("INFY", big, Decimal(1), None)], {}, "add", T2)
    assert load(app, "u")[0].quantity == big


def test_users_never_see_each_other(app):
    save(app, "a", [row("INFY", 1, 1)], {"INFY": INFY}, "replace")
    save(app, "b", [row("INFY", 5, 5)], {"INFY": INFY}, "add")
    with app() as conn:
        assert store.load_holdings(conn, "c") == []
        assert store.last_import(conn, "c") is None
        assert store.delete_portfolio(conn, "c") == 0
    assert load(app, "a")[0].quantity == 1
    assert load(app, "b")[0].quantity == 5
    with app() as conn:
        assert store.delete_portfolio(conn, "b") == 1
    assert len(load(app, "a")) == 1


def test_failed_save_changes_nothing(app, monkeypatch):
    save(app, "u", [row("INFY", 1, 1)], {"INFY": INFY}, "replace")
    real = store._merge

    def boom(*a, **k):
        raise RuntimeError("mid-way")

    monkeypatch.setattr(store, "_merge", boom)
    with pytest.raises(RuntimeError):
        save(app, "u", [row("TCS", 2, 2), row("INFY", 1, 1)], {"INFY": INFY, "TCS": TCS}, "add", T2)
    monkeypatch.setattr(store, "_merge", real)
    assert [h.symbol for h in load(app, "u")] == ["INFY"]
    with app() as conn:
        assert store.last_import(conn, "u").created_at == T1


def test_last_import_is_the_newest(app):
    save(app, "u", [row("INFY", 1, 1)], {"INFY": INFY}, "replace", T1, "one.csv")
    save(app, "u", [row("TCS", 1, 1)], {}, "add", T2, "two.csv")
    with app() as conn:
        rec = store.last_import(conn, "u")
    assert (rec.filename, rec.mode, rec.rows, rec.matched) == ("two.csv", "add", 1, 0)


def test_delete_removes_holdings_and_imports_for_that_user_only(app):
    save(app, "a", [row("INFY", 1, 1)], {"INFY": INFY}, "replace")
    save(app, "b", [row("TCS", 1, 1)], {"TCS": TCS}, "replace")
    with app() as conn:
        assert store.delete_portfolio(conn, "a") == 1
    with app() as conn:
        assert store.last_import(conn, "a") is None
        assert store.last_import(conn, "b") is not None
    assert load(app, "a") == []
    assert len(load(app, "b")) == 1


def test_tiny_quantity_is_refused_before_writing(app):
    save(app, "u", [row("INFY", 1, 1)], {"INFY": INFY}, "replace")
    with pytest.raises(ImportRefused, match="TINY: quantity is too small"):
        save(app, "u", [row("TCS", 2, 2), row("TINY", "0.00001", 1)], {}, "replace", T2)
    assert [h.symbol for h in load(app, "u")] == ["INFY"]


def test_simultaneous_adds_sum(app):
    import threading
    import time

    save(app, "u", [row("INFY", 1, 100)], {"INFY": INFY}, "replace")
    # Different symbol first so each Add inserts a brand-new key.
    errors = []
    gate = threading.Event()

    def second():
        try:
            gate.wait()
            save(app, "u", [row("NEWCO", 5, 10)], {}, "add", T2)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    th = threading.Thread(target=second)
    th.start()
    with app() as conn:
        store.save_import(conn, "u", [row("NEWCO", 3, 10)], {}, "add", "x.csv", T2)
        gate.set()
        time.sleep(0.5)  # the other Add is now waiting on the lock
    th.join(10)
    assert not errors
    got = {h.symbol: h.quantity for h in load(app, "u")}
    assert got["NEWCO"] == 8
