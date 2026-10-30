"""Verifies the sign-in tokens the site issues.

Better Auth's JWT plugin signs short-lived EdDSA tokens and publishes the
public keys as a JWKS. The API never sees a password or a session: it checks
the signature, expiry, issuer and audience, and takes `sub` as the user id.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException

from stockanalysis.config import settings

# Slack on `exp`, `iat` and `nbf`, for clock skew between the site and the API.
LEEWAY_SECONDS = 30


class AuthError(Exception):
    """The token is missing, malformed, expired or not ours."""


class AuthUnavailable(Exception):
    """Sign-in is not configured, or its keys cannot be fetched."""


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def verify(token: str) -> str:
    """Return the token's `sub` claim, or raise `AuthError`."""
    url, issuer, audience = settings.auth_jwks_url, settings.auth_issuer, settings.auth_audience
    if not (url and issuer and audience):
        raise AuthUnavailable("Sign-in is not configured on this API.")
    try:
        key = _jwks_client(url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "iss", "aud"]},
            # The site's clock and this server's can drift a little apart;
            # without slack a fresh token can read as expired or not yet issued.
            leeway=LEEWAY_SECONDS,
        )
    except jwt.PyJWKClientConnectionError as exc:
        raise AuthUnavailable("Sign-in keys are unavailable.") from exc
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("token has no subject")
    return sub


def current_user_id(authorization: Annotated[str | None, Header()] = None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() != "bearer" or not token.strip():
            if not (settings.auth_jwks_url and settings.auth_issuer and settings.auth_audience):
                raise AuthUnavailable("Sign-in is not configured on this API.")
            raise AuthError("no bearer token")
        return verify(token.strip())
    except AuthUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(
            401, "Sign in again.", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


CurrentUser = Annotated[str, Depends(current_user_id)]
