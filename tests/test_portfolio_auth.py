"""Sign-in tokens: the API accepts what the site's JWKS vouches for, and nothing else."""

from __future__ import annotations

import base64
import os
import time

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

    exists = "SELECT count(*) FROM pg_namespace WHERE nspname = 'auth'"
    with psycopg.connect(TEST_DSN, autocommit=True) as pg:
        existed = pg.execute(exists).fetchone()[0] == 1
    monkeypatch.setattr(settings, "pg_dsn", SecretStr(TEST_DSN))
    try:
        for _ in range(2):
            result = CliRunner().invoke(cli, ["setup-accounts"])
            assert result.exit_code == 0, result.output
        with psycopg.connect(TEST_DSN, autocommit=True) as pg:
            assert pg.execute(exists).fetchone()[0] == 1
    finally:
        if not existed:
            with psycopg.connect(TEST_DSN, autocommit=True) as pg:
                pg.execute("DROP SCHEMA IF EXISTS auth CASCADE")
