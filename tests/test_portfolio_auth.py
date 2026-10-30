"""Sign-in tokens: the API accepts what the site's JWKS vouches for, and nothing else."""

from __future__ import annotations

import base64
import os
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from stockanalysis.config import settings
from stockanalysis.serve import auth
from stockanalysis.serve.api import app

SITE = "http://localhost:3000"
KID = "test-key"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _public_jwk(key: Ed25519PrivateKey, kid: str) -> dict:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {
        "kty": "OKP", "crv": "Ed25519", "x": _b64(raw),
        "alg": "EdDSA", "use": "sig", "kid": kid,
    }


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def client(key, monkeypatch):
    monkeypatch.setattr(settings, "auth_jwks_url", f"{SITE}/api/auth/jwks")
    monkeypatch.setattr(settings, "auth_issuer", SITE)
    monkeypatch.setattr(settings, "auth_audience", SITE)
    monkeypatch.setattr(
        jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [_public_jwk(key, KID)]}
    )
    auth._jwks_client.cache_clear()
    yield TestClient(app)
    auth._jwks_client.cache_clear()


def _sign_with(private_key, **overrides) -> str:
    claims = {"sub": "u1", "iss": SITE, "aud": SITE, "exp": 300}
    claims.update(overrides)
    claims["exp"] = int(time.time()) + claims["exp"]
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_key, algorithm="EdDSA", headers={"kid": KID})


@pytest.fixture
def sign(key):
    return lambda **overrides: _sign_with(key, **overrides)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_returns_its_subject(client, sign):
    r = client.get("/v1/me", headers=_bearer(sign(sub="u1")))
    assert r.status_code == 200 and r.json() == {"user_id": "u1"}
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": -60},
        {"iss": "http://evil.test"},
        {"aud": "someone-else"},
        {"sub": None},
    ],
)
def test_bad_claims_are_401(client, sign, claims):
    r = client.get("/v1/me", headers=_bearer(sign(**claims)))
    assert r.status_code == 401
    assert r.json()["detail"] == "Sign in again."
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["cache-control"] == "no-store"


def test_small_clock_skew_is_tolerated(client, sign):
    """The site's clock and the API's may differ by seconds: a token just past
    its expiry, or issued a moment in the API's future, still verifies."""
    assert client.get("/v1/me", headers=_bearer(sign(exp=-10))).status_code == 200
    ahead = sign(iat=int(time.time()) + 10)
    assert client.get("/v1/me", headers=_bearer(ahead)).status_code == 200
    assert client.get("/v1/me", headers=_bearer(sign(exp=-45))).status_code == 401


def test_signature_from_another_key_is_401(client):
    forged = _sign_with(Ed25519PrivateKey.generate())
    assert client.get("/v1/me", headers=_bearer(forged)).status_code == 401


def test_garbage_token_is_401(client):
    assert client.get("/v1/me", headers=_bearer("not-a-jwt")).status_code == 401


def test_missing_or_non_bearer_header_is_401(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_unconfigured_auth_is_503(client, sign, monkeypatch):
    monkeypatch.setattr(settings, "auth_jwks_url", None)
    r = client.get("/v1/me", headers=_bearer(sign()))
    assert r.status_code == 503
    assert r.json()["detail"] == "Sign-in is not configured on this API."
    assert r.headers["cache-control"] == "no-store"


def test_unreachable_keys_are_503(client, sign, monkeypatch):
    def boom(self):
        raise jwt.PyJWKClientConnectionError("down")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", boom)
    auth._jwks_client.cache_clear()
    r = client.get("/v1/me", headers=_bearer(sign()))
    assert r.status_code == 503
    assert r.json()["detail"] == "Sign-in keys are unavailable."


def test_me_is_not_mounted_at_the_legacy_root(client):
    assert client.get("/me").status_code == 404


@pytest.mark.parametrize(
    ("path", "cache"),
    [
        ("/v1/me", "no-store"),
        ("/v1/me/portfolio", "no-store"),
        ("/v1/media", "public, max-age=300"),
        ("/v1/meetings/today", "public, max-age=300"),
    ],
)
def test_no_store_covers_me_and_below_only(monkeypatch, path, cache):
    """`/v1/me` and its sub-paths are per user; a public `/v1/media…` is not."""
    from fastapi import FastAPI

    from stockanalysis.serve.api import cache_control

    monkeypatch.setattr(settings, "api_cache_seconds", 300)
    probe = FastAPI()
    probe.middleware("http")(cache_control)
    probe.get(path)(lambda: {"ok": True})
    r = TestClient(probe).get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == cache


# ----------------------------------------------------------------------
# `stockanalysis setup-accounts`: the schema Better Auth keeps accounts in
# ----------------------------------------------------------------------

TEST_DSN = os.environ.get("SA_TEST_PG_DSN")


def test_setup_accounts_without_dsn_fails_readably(monkeypatch):
    from stockanalysis.cli import app as cli

    monkeypatch.setattr(settings, "pg_dsn", None)
    result = CliRunner().invoke(cli, ["setup-accounts"])
    assert result.exit_code == 1
    assert "SA_PG_DSN" in result.output


@pytest.mark.skipif(not TEST_DSN, reason="SA_TEST_PG_DSN not set")
def test_setup_accounts_creates_the_auth_schema_and_can_run_again(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from stockanalysis.cli import app as cli

    app_schema = f"sa_test_app_{uuid.uuid4().hex[:8]}"
    exists = "SELECT count(*) FROM pg_namespace WHERE nspname = %s"
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        existed = pg.execute(exists, ["auth"]).fetchone()[0] == 1
    monkeypatch.setattr(settings, "pg_dsn", SecretStr(TEST_DSN))
    monkeypatch.setattr(settings, "pg_app_schema", app_schema)
    try:
        for _ in range(2):
            result = CliRunner().invoke(cli, ["setup-accounts"])
            assert result.exit_code == 0, result.output
        with psycopg.connect(TEST_DSN, autocommit=True) as pg:
            assert pg.execute(exists, ["auth"]).fetchone()[0] == 1
            assert pg.execute(exists, [app_schema]).fetchone()[0] == 1
    finally:
        with psycopg.connect(TEST_DSN, autocommit=True) as pg:
            pg.execute(f'DROP SCHEMA IF EXISTS "{app_schema}" CASCADE')
            if not existed:
                pg.execute("DROP SCHEMA IF EXISTS auth CASCADE")


# ----------------------------------------------------------------------
# The default DuckDB deployment: psycopg is the optional `postgres` extra
# ----------------------------------------------------------------------

_WITHOUT_PSYCOPG = """
import json, sys

class _NoPsycopg:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("psycopg", "psycopg_pool"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, _NoPsycopg())

from stockanalysis.serve.api import app
assert "/v1/me/portfolio" in app.openapi()["paths"]

from typer.testing import CliRunner
from stockanalysis.cli import app as cli
result = CliRunner().invoke(cli, ["export-openapi", sys.argv[1]])
assert result.exit_code == 0, result.output
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("psycopg", "psycopg_pool"))
assert not loaded, loaded
print(json.dumps(len(json.load(open(sys.argv[1]))["paths"])))
"""


def test_api_and_openapi_export_need_no_psycopg(tmp_path):
    """The API (portfolio routes included) imports and builds without psycopg."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("SA_")}
    out = tmp_path / "openapi.json"
    # cwd is tmp_path, so no developer .env is read.
    done = subprocess.run(
        [sys.executable, "-c", _WITHOUT_PSYCOPG, str(out)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    assert int(done.stdout.strip().splitlines()[-1]) > 0
