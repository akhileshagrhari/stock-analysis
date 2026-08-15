"""Survivorship bias tests.

A universe built from today's constituents has silently deleted every company
that collapsed. These tests assert that a company which dies mid-backtest is
(a) present in the universe before it dies, (b) absent after, and (c) has its
losses actually counted rather than quietly dropped.

(c) is the one that matters most and is easiest to get wrong: a naive
implementation joins on price data, finds none for the dead company, and drops
the row — deleting the loss from the P&L.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from stockanalysis.db.database import Database
from tests.conftest import make_instruments, make_membership, make_prices


def test_delisted_company_present_before_and_absent_after(db: Database):
    delist_date = dt.date(2022, 6, 1)
    instruments = make_instruments(5, delisted={2: delist_date})
    db.upsert_df("instruments", instruments, ["isin"])
    db.upsert_df(
        "index_membership",
        make_membership(instruments["isin"].tolist(), "IDX", dt.date(2019, 1, 1)),
        ["index_name", "isin", "from_date"],
    )

    dead = "INE000000002"
    assert dead in db.as_of_universe("IDX", dt.date(2022, 5, 31)), (
        "company delisted in June must still be in the May universe — it was "
        "tradeable then, and excluding it is survivorship bias"
    )
    assert dead not in db.as_of_universe("IDX", delist_date)
    assert dead not in db.as_of_universe("IDX", dt.date(2023, 1, 1))


def test_delisted_company_losses_are_counted(db: Database):
    """A name that stops trading is marked to its last price, not dropped."""
    delist_date = dt.date(2022, 6, 1)
    instruments = make_instruments(3, delisted={1: delist_date})
    db.upsert_df("instruments", instruments, ["isin"])

    isins = instruments["isin"].tolist()
    dead = "INE000000001"

    prices = make_prices(
        isins, dt.date(2022, 1, 1), dt.date(2022, 12, 31),
        delisting={dead: delist_date},
    )
    # Force the dying company into a collapse before it stops trading.
    mask = prices["isin"] == dead
    prices.loc[mask, "adj_close"] = prices.loc[mask, "adj_close"] * 0.2
    db.upsert_df("prices_daily", prices, ["isin", "date"])

    rets = db.forward_returns(isins, dt.date(2022, 1, 31), dt.date(2022, 5, 31))

    assert dead in rets.index, (
        "delisted company vanished from forward returns — its loss was silently "
        "deleted from the backtest"
    )
    assert rets[dead] < 0


def test_membership_coverage_flag_gates_the_warning(db: Database):
    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)
    instruments = make_instruments(3)
    db.upsert_df("instruments", instruments, ["isin"])
    db.upsert_df(
        "index_membership",
        make_membership(instruments["isin"].tolist(), "IDX", start),
        ["index_name", "isin", "from_date"],
    )

    # No coverage row → snapshot only → unsafe.
    assert not db.membership_is_survivorship_safe("IDX", start, end)

    db.upsert_df(
        "index_membership_coverage",
        pd.DataFrame([{
            "index_name": "IDX",
            "verified_from": start,
            "verified_to": end,
            "source": "TEST",
            "loaded_at": dt.datetime.now(),
        }]),
        ["index_name", "verified_from"],
    )
    assert db.membership_is_survivorship_safe("IDX", start, end)

    # Partial coverage must not count as safe.
    assert not db.membership_is_survivorship_safe("IDX", dt.date(2018, 1, 1), end)


def test_membership_interval_is_half_open(db: Database):
    """Leaving the index on date X means a member on X-1, not on X."""
    instruments = make_instruments(2)
    db.upsert_df("instruments", instruments, ["isin"])
    db.upsert_df(
        "index_membership",
        pd.DataFrame([{
            "index_name": "IDX",
            "isin": "INE000000000",
            "from_date": dt.date(2020, 1, 1),
            "to_date": dt.date(2022, 4, 1),
        }]),
        ["index_name", "isin", "from_date"],
    )

    assert "INE000000000" in db.as_of_universe("IDX", dt.date(2022, 3, 31))
    assert "INE000000000" not in db.as_of_universe("IDX", dt.date(2022, 4, 1))
