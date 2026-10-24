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

import pandas as pd
import pytest
from test_phase4_serving import AS_OF, INDEX, _seed_scored

from stockanalysis.db.database import Database
from stockanalysis.factors.composite import CompositeModel, ScoringConfig, persist
from stockanalysis.factors.panel import PANEL_CACHE
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


# ----------------------------------------------------------------------
# R2 — why a company isn't scored
# ----------------------------------------------------------------------

BANK_FILING = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml"
)


def _rescore(db: Database, monkeypatch) -> None:
    """Score AS_OF again, as a pipeline run would, over whatever the data now is."""
    monkeypatch.setattr(PANEL_CACHE, "_panel", None)
    isins = db.as_of_universe(INDEX, AS_OF)
    persist(db, CompositeModel().score(db, isins, AS_OF))


def _status(client, isin: str) -> dict:
    response = client.get(f"/v1/instruments/{isin}/score-status")
    assert response.status_code == 200, response.text
    return response.json()


class TestR2WhyACompanyIsntScored:
    def test_a_scored_company_says_so_with_the_coverage_it_was_scored_on(
        self, client, two_dates_db
    ):
        isin = _isin_scored_on_both(two_dates_db)
        body = _status(client, isin)
        stored = two_dates_db.query(
            "SELECT coverage FROM signals WHERE isin = ? AND as_of_date = ?",
            [isin, AS_OF],
        )["coverage"].iloc[0]
        assert body["scored"] is True
        assert body["reason"] == "scored"
        assert body["latest_as_of"] == body["measured_as_of"] == AS_OF.isoformat()
        assert body["coverage"] == pytest.approx(stored)
        assert body["coverage_floor"] == ScoringConfig().min_coverage
        assert body["factors_total"] == len(CompositeModel().factors)
        assert "31 Jan 2023" in body["detail"]

    def test_the_replayed_coverage_is_the_one_the_run_stored(self, two_dates_db):
        """The unscored path reports a replayed coverage; it must be the model's."""
        stored = two_dates_db.query(
            "SELECT isin, coverage FROM signals WHERE as_of_date = ?", [AS_OF]
        )
        two_dates_db.conn.execute("DELETE FROM signals WHERE as_of_date = ?", [AS_OF])
        for row in stored.itertuples():
            status = explain.score_status(two_dates_db, row.isin)
            assert status.measured_as_of == AS_OF
            assert status.coverage == pytest.approx(row.coverage)

    def test_a_company_the_latest_run_did_not_measure_keeps_its_older_score(
        self, client, two_dates_db
    ):
        isin = _isin_scored_on_both(two_dates_db)
        for table in ("signals", "factor_scores"):
            two_dates_db.conn.execute(
                f"DELETE FROM {table} WHERE isin = ? AND as_of_date = ?", [isin, AS_OF]
            )
        body = _status(client, isin)
        assert body["scored"] is True
        assert body["reason"] == "not_in_latest_run"
        assert body["latest_as_of"] == body["measured_as_of"] == EARLIER.isoformat()
        assert "30 Dec 2022" in body["detail"] and "31 Jan 2023" in body["detail"]

    def test_too_little_data_is_below_the_floor_and_names_what_was_missing(
        self, client, two_dates_db, monkeypatch
    ):
        isin = _isin_scored_on_both(two_dates_db)
        two_dates_db.conn.execute("DELETE FROM prices_daily WHERE isin = ?", [isin])
        _rescore(two_dates_db, monkeypatch)

        body = _status(client, isin)
        assert body["scored"] is False
        assert body["reason"] == "below_coverage_floor"
        assert body["coverage"] < body["coverage_floor"]
        # Its last score stands in `signals`; the newest run is what failed it.
        assert body["latest_as_of"] == EARLIER.isoformat()
        assert body["measured_as_of"] == AS_OF.isoformat()
        assert "value" in body["detail"] and "momentum" in body["detail"]
        assert f"{body['coverage']:.0%}" in body["detail"]
        assert client.get(f"/v1/instruments/{isin}/latest").json()["as_of"] == (
            EARLIER.isoformat()
        )

    def test_no_annual_results_is_missing_fundamentals(
        self, client, two_dates_db, monkeypatch
    ):
        isin = _isin_scored_on_both(two_dates_db)
        two_dates_db.conn.execute("DELETE FROM fundamentals_annual WHERE isin = ?", [isin])
        _rescore(two_dates_db, monkeypatch)

        body = _status(client, isin)
        assert body["scored"] is False
        assert body["reason"] == "missing_fundamentals"
        assert "No annual results" in body["detail"]
        assert body["factors_measured"] < body["factors_total"]

    def test_a_bank_is_a_sector_the_model_cannot_read(
        self, client, two_dates_db, monkeypatch
    ):
        isin = _isin_scored_on_both(two_dates_db)
        two_dates_db.conn.execute("DELETE FROM fundamentals_annual WHERE isin = ?", [isin])
        two_dates_db.upsert_df(
            "fundamentals_quarterly",
            pd.DataFrame([{
                "isin": isin,
                "period_end_date": dt.date(2022, 9, 30),
                "filing_date": dt.date(2022, 10, 20),
                "revenue": None,
                "pat": 120.0,
                "eps": 3.1,
                "xbrl_url": BANK_FILING,
            }]),
            ["isin", "period_end_date"],
        )
        _rescore(two_dates_db, monkeypatch)

        body = _status(client, isin)
        assert body["reason"] == "sector_not_supported"
        assert "banks'" in body["detail"]

    def test_the_results_format_is_read_from_the_exchanges_file_name(self):
        assert explain.results_format(BANK_FILING) == "INTEGRATED_FILING_BANKING"
        assert explain._unsupported(BANK_FILING) == "banks"
        assert explain._unsupported(
            "https://nsearchives.nseindia.com/corporate/xbrl/"
            "INTEGRATED_FILING_LI_1739360_15102026072721_WEB.xml"
        ) == "insurers"
        # An NBFC's format reads fine, and so does the legacy Ind AS one.
        for url in (
            "https://nsearchives.nseindia.com/corporate/xbrl/NBFC_INDAS_1_2.xml",
            "https://nsearchives.nseindia.com/corporate/xbrl/"
            "INTEGRATED_FILING_INDAS_1701291_24072026084853_WEB.xml",
            None,
        ):
            assert explain._unsupported(url) is None

    def test_a_company_no_run_has_measured_says_what_it_lacks(self, client, two_dates_db):
        two_dates_db.conn.execute(
            "INSERT INTO instruments (isin, nse_symbol, name) "
            "VALUES ('INE00NEW0001', 'NEWCO', 'New Listing Ltd')"
        )
        body = _status(client, "INE00NEW0001")
        assert body == {
            "isin": "INE00NEW0001",
            "scored": False,
            "reason": "never_scored",
            "latest_as_of": None,
            "measured_as_of": None,
            "coverage": None,
            "coverage_floor": ScoringConfig().min_coverage,
            "factors_measured": None,
            "factors_total": len(CompositeModel().factors),
            "detail": "No scoring run has measured this company; no prices or "
            "results are stored for it.",
        }

    def test_an_unknown_isin_is_404(self, client):
        assert client.get("/v1/instruments/INE_MISSING/score-status").status_code == 404

    def test_the_api_enum_is_the_reasons_the_reader_can_give(self):
        from typing import get_args

        from stockanalysis.serve.api import ScoreReason, app

        assert set(get_args(ScoreReason)) == set(explain.SCORE_REASONS)
        schema = app.openapi()["components"]["schemas"]["ScoreStatusOut"]
        assert set(schema["properties"]["reason"]["enum"]) == set(explain.SCORE_REASONS)


# ----------------------------------------------------------------------
# R3 — the model catalogue
# ----------------------------------------------------------------------


class TestR3ModelCatalogue:
    def test_the_lists_are_the_python_definitions_exactly(self, client):
        from stockanalysis.factors import redflags
        from stockanalysis.factors.composite import default_factors

        body = client.get("/v1/model").json()
        config = ScoringConfig()
        assert body["version"] == config.version()
        assert body["coverage_floor"] == config.min_coverage

        assert [f["family"] for f in body["families"]] == explain.FAMILY_ORDER
        assert {f["family"]: f["weight"] for f in body["families"]} == body["family_weights"]
        assert all(f["meaning"] == explain.FAMILY_MEANING[f["family"]] for f in body["families"])

        assert body["factors"] == [
            {
                "name": f.name,
                "label": explain.FACTOR_LABELS[f.name][0],
                "meaning": explain.FACTOR_LABELS[f.name][1],
                "family": f.family,
                "unit": explain.FACTOR_UNITS[f.name],
                "higher_is_better": f.higher_is_better,
            }
            for f in default_factors()
        ]
        assert body["red_flags"] == [
            {"name": d.name, "description": d.description, "reachable": d.reachable}
            for d in redflags.DEFINITIONS
        ]

    def test_a_factor_added_in_python_must_be_labelled_and_given_a_unit(self):
        """Otherwise the catalogue would show it under a made-up label."""
        from stockanalysis.factors.composite import default_factors

        names = [f.name for f in default_factors()]
        assert set(explain.FACTOR_LABELS) == set(names)
        assert set(explain.FACTOR_UNITS) == set(names)
        assert set(explain.FACTOR_UNITS.values()) <= set(explain.UNITS)

    def test_the_api_unit_enum_is_the_units_the_reader_assigns(self):
        from typing import get_args

        from stockanalysis.serve.api import FactorUnit

        assert set(get_args(FactorUnit)) == set(explain.UNITS)

    def test_units_follow_the_definitions_not_the_names(self, client):
        units = {f["name"]: f["unit"] for f in client.get("/v1/model").json()["factors"]}
        assert units["earnings_yield"] == "percent"
        assert units["book_to_price"] == "multiple"
        assert units["margin_trend"] == "pp"
        # A return minus the universe's median return: percentage points.
        assert units["relative_strength_6m"] == "pp"
        # E/P × growth: a product of two fractions, not a multiple.
        assert units["peg_inverse"] == "ratio"
        assert units["price_to_200dma"] == "ratio_vs_trend"
        assert units["news_sentiment_30d"] == "score"

    def test_the_lower_is_better_metrics_say_so(self, client):
        lower = {
            f["name"] for f in client.get("/v1/model").json()["factors"]
            if not f["higher_is_better"]
        }
        assert lower == {"debt_to_equity", "accruals"}

    def test_unreachable_flags_are_marked(self, client):
        flags = {f["name"]: f["reachable"] for f in client.get("/v1/model").json()["red_flags"]}
        assert len(flags) == 6
        assert {name for name, ok in flags.items() if not ok} == {
            "promoter_pledge", "rating_downgrade"
        }

    def test_drivers_and_stored_factors_carry_their_unit(self, client, two_dates_db):
        isin = _isin_scored_on_both(two_dates_db)
        explanation = client.get(f"/v1/instruments/{isin}/explanation").json()
        drivers = explanation["strengths"] + explanation["weaknesses"]
        assert drivers
        assert all(d["unit"] == explain.FACTOR_UNITS[d["name"]] for d in drivers)

        latest = client.get(f"/v1/instruments/{isin}/latest").json()
        assert latest["factors"]
        assert all(
            f["unit"] == explain.FACTOR_UNITS[f["factor_name"]] for f in latest["factors"]
        )

    def test_a_stored_factor_the_model_no_longer_defines_has_no_unit(self):
        from stockanalysis.serve import queries
        from stockanalysis.serve.api import FactorOut

        row = queries.FactorScore(factor_name="retired_factor", raw_value=1.0, sector_zscore=0.1)
        assert FactorOut.of(row).unit is None
