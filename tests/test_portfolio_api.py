# ruff: noqa: F811
"""Import and delete routes — need SA_TEST_PG_DSN, like the Postgres serving tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from test_portfolio_auth import _bearer, client, key, sign  # noqa: F401  (fixtures)
from test_postgres_serving import (  # noqa: F401  (fixtures)
    TEST_DSN,
    needs_pg,
    pg_schema,
    published,
    site,
)

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.portfolio import store
from stockanalysis.portfolio.kite import MAX_BYTES, NOT_KITE
from stockanalysis.serve import queries

FIXTURE = Path(__file__).parent / "fixtures" / "portfolio" / "kite_holdings.csv"
SMALL = (
    b'"Instrument","Qty.","Avg. cost","LTP"\n'
    b'"TEST001",10,100.5,110\n'
    b'"NIFTYBEES",5,200,210\n'
)
pytestmark = needs_pg
URL = "/v1/me/portfolio/import"


@pytest.fixture
def api(client, published, monkeypatch):
    import psycopg
    from psycopg import sql

    from stockanalysis.db.postgres import close_reader_pools

    name = f"apptest_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "serve_backend", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", SecretStr(TEST_DSN))
    monkeypatch.setattr(settings, "pg_app_schema", name)
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        pg.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        store.ensure_schema(pg)
    yield client
    close_reader_pools()
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        pg.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _post(api, sign, body, mode="replace", user="u1", **params):
    return api.post(
        URL, params={"mode": mode, **params}, content=body, headers=_bearer(sign(sub=user))
    )


def _holdings(user):
    from stockanalysis.db.postgres import app_connection

    with app_connection() as conn:
        return store.load_holdings(conn, user)


def test_import_replace_then_read_back(api, sign, site):
    body = FIXTURE.read_bytes()
    from stockanalysis.portfolio.kite import parse_kite_holdings

    parsed = parse_kite_holdings(body)
    with Database(site.path, read_only=True) as db:
        expected = len(queries.resolve_symbols(db, [r.symbol for r in parsed.rows]))
    r = _post(api, sign, body, filename="my.csv")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rows"] == len(parsed.rows) == 23
    assert out["matched"] == expected
    assert out["filename"] == "my.csv" and out["mode"] == "replace"
    assert r.headers["cache-control"] == "no-store"
    assert len(_holdings("u1")) == 23

    r = _post(api, sign, SMALL)
    assert r.status_code == 200 and r.json()["matched"] == 1
    held = _holdings("u1")
    assert {h.symbol for h in held} == {"TEST001", "NIFTYBEES"}
    assert next(h for h in held if h.symbol == "TEST001").isin == "INE000000001"


def test_add_twice_doubles_quantities(api, sign):
    assert _post(api, sign, SMALL, mode="add").status_code == 200
    assert _post(api, sign, SMALL, mode="add").status_code == 200
    assert {h.symbol: h.quantity for h in _holdings("u1")} == {
        "TEST001": Decimal("20"), "NIFTYBEES": Decimal("10"),
    }


def test_refused_file_changes_nothing(api, sign):
    _post(api, sign, FIXTURE.read_bytes())
    before = _holdings("u1")
    r = _post(api, sign, b"PK\x03\x04")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["message"] == NOT_KITE and detail["problems"] == []
    assert _holdings("u1") == before


def test_body_over_limit_is_413(api, sign):
    r = _post(api, sign, b"x" * (MAX_BYTES + 1))
    assert r.status_code == 413
    assert r.json()["detail"] == "The file is larger than 1 MB."


def test_bad_mode_is_422(api, sign):
    assert _post(api, sign, SMALL, mode="merge").status_code == 422


def test_other_users_portfolio_untouched(api, sign):
    _post(api, sign, SMALL, user="a")
    h = _bearer(sign(sub="b"))
    r = api.delete("/v1/me/portfolio", headers=h)
    assert r.status_code == 200 and r.json() == {"deleted": 0}
    assert len(_holdings("a")) == 2
    r = api.delete("/v1/me/portfolio", headers=_bearer(sign(sub="a")))
    assert r.json() == {"deleted": 2} and _holdings("a") == []


def test_duckdb_backend_is_503(api, sign, monkeypatch):
    monkeypatch.setattr(settings, "serve_backend", "duckdb")
    r = _post(api, sign, SMALL)
    assert r.status_code == 503 and r.json()["detail"] == "Portfolio needs Postgres."
    r = api.delete("/v1/me/portfolio", headers=_bearer(sign()))
    assert r.status_code == 503


def test_no_token_is_401(api):
    assert api.post(URL, params={"mode": "replace"}, content=SMALL).status_code == 401
    assert api.delete("/v1/me/portfolio").status_code == 401


def test_resolve_symbols_duckdb(site):
    with Database(site.path, read_only=True) as db:
        got = queries.resolve_symbols(db, ["test001", "NIFTYBEES", "TEST001"])
        assert set(got) == {"TEST001"} and got["TEST001"].isin == "INE000000001"
        assert queries.resolve_symbols(db, []) == {}


def test_resolve_symbols_postgres(published):
    from stockanalysis.db.postgres import pooled_reader

    with pooled_reader() as db:
        got = queries.resolve_symbols(db, ["test001", "NIFTYBEES"])
    assert set(got) == {"TEST001"}
