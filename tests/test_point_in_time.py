"""Lookahead bias tests.

The strongest of these is `test_factor_value_is_invariant_to_future_data`:
it computes a factor, then inserts data from *after* the decision date, then
recomputes and asserts the answer did not move. Any leak — however indirect —
changes the second number. This is the test that would have caught the bug
before it flattered three months of results.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from stockanalysis.db.database import Database
from stockanalysis.factors.momentum import Momentum12_1
from tests.conftest import make_instruments, make_membership, make_prices


def test_fundamentals_hidden_until_filing_date(db: Database):
    """FY2023 numbers were not knowable in May 2023 — the report filed in July."""
    db.upsert_df("instruments", make_instruments(1), ["isin"])
    db.upsert_df(
        "fundamentals_annual",
        pd.DataFrame([{
            "isin": "INE000000000",
            "fiscal_year": 2023,
            "period_end_date": dt.date(2023, 3, 31),
            "filing_date": dt.date(2023, 7, 15),
            "basis": "CONSOLIDATED",
            "revenue": 1000.0, "ebitda": 200.0, "pat": 120.0, "eps": 12.0,
            "ocf": 150.0, "fcf": 100.0, "capex": 50.0,
            "total_assets": 2000.0, "total_equity": 1200.0,
            "total_debt": 500.0, "cash": 300.0,
            "interest_expense": 40.0, "tax_expense": 30.0,
            "contingent_liabilities": 100.0, "auditor_opinion": "UNQUALIFIED",
            "extraction_confidence": 1.0, "source_filing_id": "F1",
        }]),
        ["isin", "fiscal_year", "basis"],
    )

    # Period has ended, report not yet filed — must be invisible.
    assert db.as_of_fundamentals(["INE000000000"], dt.date(2023, 5, 1)).empty
    assert db.as_of_fundamentals(["INE000000000"], dt.date(2023, 7, 14)).empty

    # Filed — now visible.
    visible = db.as_of_fundamentals(["INE000000000"], dt.date(2023, 7, 15))
    assert len(visible) == 1
    assert visible.iloc[0]["revenue"] == 1000.0


def test_as_of_prices_never_returns_future_rows(db: Database):
    isins = ["INE000000000"]
    db.upsert_df("instruments", make_instruments(1), ["isin"])
    db.upsert_df(
        "prices_daily",
        make_prices(isins, dt.date(2022, 1, 1), dt.date(2023, 12, 31)),
        ["isin", "date"],
    )

    cutoff = dt.date(2023, 1, 15)
    got = db.as_of_prices(isins, cutoff, lookback_days=800)
    assert not got.empty
    assert pd.to_datetime(got["date"]).max().date() <= cutoff


def test_factor_value_is_invariant_to_future_data(db: Database):
    """Compute -> inject the future -> recompute -> must be identical.

    This catches leaks the other tests cannot, because it makes no assumption
    about *how* the leak occurs. If any code path reaches past the as-of cutoff,
    the recomputed value moves.
    """
    instruments = make_instruments(12)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()

    decision_date = dt.date(2022, 6, 30)

    past = make_prices(isins, dt.date(2020, 1, 1), decision_date, seed=7)
    db.upsert_df("prices_daily", past, ["isin", "date"])

    factor = Momentum12_1()
    before = factor.compute(db, isins, decision_date)

    # Inject wildly divergent future prices. A leaky factor cannot ignore these.
    future = make_prices(
        isins, decision_date + dt.timedelta(days=1), dt.date(2023, 6, 30), seed=999
    )
    future["adj_close"] *= 5.0
    future["close"] *= 5.0
    db.upsert_df("prices_daily", future, ["isin", "date"])

    after = factor.compute(db, isins, decision_date)

    pd.testing.assert_series_equal(
        before.dropna(), after.dropna(), check_exact=False, rtol=1e-12,
        obj="momentum factor leaked future data",
    )


def test_universe_excludes_not_yet_listed(db: Database):
    instruments = make_instruments(3)
    instruments.loc[1, "listing_date"] = dt.date(2023, 6, 1)
    db.upsert_df("instruments", instruments, ["isin"])
    db.upsert_df(
        "index_membership",
        make_membership(instruments["isin"].tolist(), "IDX", dt.date(2019, 1, 1)),
        ["index_name", "isin", "from_date"],
    )

    assert "INE000000001" not in db.as_of_universe("IDX", dt.date(2023, 1, 1))
    assert "INE000000001" in db.as_of_universe("IDX", dt.date(2023, 6, 1))


def test_shareholding_respects_disclosure_date(db: Database):
    db.upsert_df("instruments", make_instruments(1), ["isin"])
    db.upsert_df(
        "shareholding",
        pd.DataFrame([{
            "isin": "INE000000000",
            "quarter_end": dt.date(2023, 3, 31),
            "disclosed_date": dt.date(2023, 4, 21),
            "promoter_pct": 55.0, "promoter_pledged_pct": 12.0,
            "fii_pct": 20.0, "dii_pct": 15.0, "public_pct": 10.0,
        }]),
        ["isin", "quarter_end"],
    )

    assert db.as_of_shareholding(["INE000000000"], dt.date(2023, 4, 1)).empty
    assert len(db.as_of_shareholding(["INE000000000"], dt.date(2023, 4, 21))) == 1


@pytest.mark.parametrize("cutoff_day", [10, 20, 28])
def test_prices_cutoff_is_inclusive_boundary(db: Database, cutoff_day: int):
    isins = ["INE000000000"]
    db.upsert_df("instruments", make_instruments(1), ["isin"])
    db.upsert_df(
        "prices_daily",
        make_prices(isins, dt.date(2023, 1, 1), dt.date(2023, 3, 31)),
        ["isin", "date"],
    )
    cutoff = dt.date(2023, 2, cutoff_day)
    got = db.as_of_prices(isins, cutoff)
    if not got.empty:
        assert pd.to_datetime(got["date"]).max().date() <= cutoff
