"""What the public site asked of the API.

The requirements are docs/superpowers/specs/2026-10-24-site-api-requirements.md
(on the UI branch); their status is in ...-requirements-status.md beside it.

One class per requirement (R1-R17), against the same scored fixture the phase-4
serving tests use. Parity with Postgres is checked separately, for every route,
by `test_postgres_serving.PARITY_URLS`; these tests are about what the answers
mean.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_phase4_serving import AS_OF, INDEX, _seed_scored

from stockanalysis.db.database import Database
from stockanalysis.factors.composite import CompositeModel, ScoringConfig, persist
from stockanalysis.serve import explain

EARLIER = dt.date(2022, 12, 30)


@pytest.fixture
def scored_db(seeded_db: Database) -> Database:
    return _seed_scored(seeded_db)


@pytest.fixture
def two_dates_db(scored_db: Database) -> Database:
    """The fixture scored a second time, a month earlier, so there is a past."""
    isins = scored_db.as_of_universe(INDEX, EARLIER)
    result = CompositeModel(config=ScoringConfig(min_coverage=0.0)).score(
        scored_db, isins, EARLIER
    )
    persist(scored_db, result)
    return scored_db


def _client(db: Database):
    from fastapi.testclient import TestClient

    from stockanalysis.serve.api import app, get_db

    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture
def client(two_dates_db: Database):
    try:
        yield _client(two_dates_db)
    finally:
        from stockanalysis.serve.api import app

        app.dependency_overrides.clear()


def _isin_scored_on_both(db: Database) -> str:
    df = db.query(
        "SELECT isin FROM signals WHERE as_of_date IN (?, ?) "
        "GROUP BY isin HAVING COUNT(*) = 2 ORDER BY isin LIMIT 1",
        [EARLIER, AS_OF],
    )
    return df["isin"].iloc[0]


# ----------------------------------------------------------------------
# R1 — explanation for a past date
# ----------------------------------------------------------------------


class TestR1ExplanationForAPastDate:
    def test_a_past_date_is_explained_from_that_dates_rows(self, client, two_dates_db):
        isin = _isin_scored_on_both(two_dates_db)
        body = client.get(f"/v1/instruments/{isin}/explanation?as_of={EARLIER}").json()

        stored = two_dates_db.query(
            "SELECT composite_score FROM signals WHERE isin = ? AND as_of_date = ?",
            [isin, EARLIER],
        )["composite_score"].iloc[0]
        expected = explain.explain(two_dates_db, isin, EARLIER)
        assert body["as_of"] == EARLIER.isoformat()
        assert body["composite_score"] == pytest.approx(stored)
        assert [f["percentile"] for f in body["families"]] == [
            f.percentile for f in expected.families
        ]
        assert [d["name"] for d in body["strengths"]] == [d.name for d in expected.strengths]

    def test_the_past_and_the_latest_differ_where_the_data_did(self, client, two_dates_db):
        isin = _isin_scored_on_both(two_dates_db)
        then = client.get(f"/v1/instruments/{isin}/explanation?as_of={EARLIER}").json()
        now = client.get(f"/v1/instruments/{isin}/explanation").json()
        assert now["as_of"] == AS_OF.isoformat()
        assert then["composite_score"] != now["composite_score"]
        # Not the latest signal's headline pasted onto another day's factors.
        assert f"{then['composite_score']:.0f}/100" in then["headline"]

    def test_a_date_with_no_row_is_404_not_the_nearest(self, client, two_dates_db):
        isin = _isin_scored_on_both(two_dates_db)
        response = client.get(f"/v1/instruments/{isin}/explanation?as_of=2023-01-02")
        assert response.status_code == 404
        assert "2023-01-02" in response.json()["detail"]

    def test_stale_semantics_hold_for_a_past_date(self, client, two_dates_db):
        isin = _isin_scored_on_both(two_dates_db)
        two_dates_db.conn.execute(
            "UPDATE signals SET model_version = 'phase2-composite-oldmodel' "
            "WHERE isin = ? AND as_of_date = ?",
            [isin, EARLIER],
        )
        body = client.get(f"/v1/instruments/{isin}/explanation?as_of={EARLIER}").json()
        assert body["stale"] is True
        assert body["stored_version"] == "phase2-composite-oldmodel"
        assert body["current_version"] == ScoringConfig().version()

    def test_a_malformed_date_is_422(self, client, two_dates_db):
        isin = _isin_scored_on_both(two_dates_db)
        response = client.get(f"/v1/instruments/{isin}/explanation?as_of=31-07-2026")
        assert response.status_code == 422
