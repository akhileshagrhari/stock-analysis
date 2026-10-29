"""The signed-in `/v1/me*` routes.

Imported at the end of `serve.api`, so these register on the same `app`. They
are mounted at `/v1/me*` only (no legacy root twin, unlike the public routes),
and the `cache_control` middleware marks every one `no-store`.
"""

from __future__ import annotations

from pydantic import BaseModel

from stockanalysis.serve.api import app
from stockanalysis.serve.auth import CurrentUser

# GET templates that depend on who is asking. They read the unpublished `app`
# schema, so the DuckDB/Postgres parity test has nothing to compare.
PER_USER_PATHS: frozenset[str] = frozenset({"/v1/me"})


class Me(BaseModel):
    user_id: str


@app.get("/v1/me", tags=["account"], response_model=Me)
def me(user_id: CurrentUser) -> Me:
    return Me(user_id=user_id)
