"""Cost model and factor mechanics."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from stockanalysis.backtest.costs import (
    compute_costs,
    costs_from_weight_change,
)
from stockanalysis.config import CostModel
from stockanalysis.db.database import Database
from stockanalysis.factors.base import sector_zscore, winsorize
from stockanalysis.factors.momentum import Momentum12_1
from tests.conftest import make_instruments, make_prices

# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------

def test_stamp_duty_applies_only_to_buys():
    m = CostModel()
    buy_only = compute_costs(buy_value=1_000_000, sell_value=0, model=m)
    sell_only = compute_costs(buy_value=0, sell_value=1_000_000, model=m)
    assert buy_only.statutory > sell_only.statutory
    expected_gap = 1_000_000 * m.stamp_duty_buy
    assert np.isclose(buy_only.statutory - sell_only.statutory, expected_gap)


def test_gst_excludes_stt_and_stamp_duty():
    """GST applies to brokerage and exchange fees, not to statutory taxes."""
    m = CostModel(brokerage_pct=0.0003)
    c = compute_costs(buy_value=1_000_000, sell_value=1_000_000, model=m)

    turnover = 2_000_000
    exch_sebi = turnover * (m.exchange_txn + m.sebi_turnover)
    brokerage = turnover * m.brokerage_pct
    assert np.isclose(c.brokerage_and_gst, (exch_sebi + brokerage) * 1.18)


def test_slippage_scales_with_participation():
    m = CostModel()
    small = compute_costs(
        buy_value=1_000_000, sell_value=0,
        participation=pd.Series([0.001]), model=m,
    )
    large = compute_costs(
        buy_value=1_000_000, sell_value=0,
        participation=pd.Series([0.10]), model=m,
    )
    assert large.slippage > small.slippage * 5


def test_zero_turnover_is_free():
    w = pd.Series({"A": 0.5, "B": 0.5})
    c = costs_from_weight_change(w, w, portfolio_value=1_000_000)
    assert c.turnover == 0
    assert np.isclose(c.total, 0.0)


def test_full_rotation_costs_more_than_partial():
    old = pd.Series({"A": 0.5, "B": 0.5})
    partial = pd.Series({"A": 0.5, "C": 0.5})
    full = pd.Series({"C": 0.5, "D": 0.5})

    c_partial = costs_from_weight_change(old, partial, 1_000_000)
    c_full = costs_from_weight_change(old, full, 1_000_000)
    assert c_full.total > c_partial.total


# --------------------------------------------------------------------------
# Factor mechanics
# --------------------------------------------------------------------------

def test_winsorize_clips_outlier():
    s = pd.Series([1, 2, 3, 4, 5, 1000.0])
    assert winsorize(s).max() < 1000.0


def test_sector_zscore_is_computed_within_sector():
    """Two sectors on different scales must both centre near zero."""
    values = pd.Series({f"A{i}": 10 + i for i in range(6)}
                       | {f"B{i}": 1000 + i for i in range(6)})
    sectors = pd.Series({f"A{i}": "IT" for i in range(6)}
                        | {f"B{i}": "Banks" for i in range(6)})

    z = sector_zscore(values, sectors, min_sector_size=5)
    assert abs(z[[f"A{i}" for i in range(6)]].mean()) < 1e-9
    assert abs(z[[f"B{i}" for i in range(6)]].mean()) < 1e-9


def test_small_sector_falls_back_to_universe():
    """A z-score over three observations is noise; fall back rather than pretend."""
    values = pd.Series({f"A{i}": float(i) for i in range(10)} | {"B0": 5.0, "B1": 6.0})
    sectors = pd.Series({f"A{i}": "IT" for i in range(10)} | {"B0": "Tiny", "B1": "Tiny"})
    z = sector_zscore(values, sectors, min_sector_size=5)
    assert z.notna().all()


def test_momentum_skips_the_recent_month(db: Database):
    """A spike inside the skip window must not register."""
    instruments = make_instruments(1)
    db.upsert_df("instruments", instruments, ["isin"])
    isin = "INE000000000"

    as_of = dt.date(2023, 6, 30)
    prices = make_prices([isin], dt.date(2022, 1, 1), as_of, seed=3)
    prices["adj_close"] = 100.0  # perfectly flat
    prices["close"] = 100.0

    # Spike only in the final three weeks — inside the skipped month.
    recent = pd.to_datetime(prices["date"]) > pd.Timestamp(as_of) - pd.Timedelta(days=21)
    prices.loc[recent, "adj_close"] = 500.0
    db.upsert_df("prices_daily", prices, ["isin", "date"])

    value = Momentum12_1().compute(db, [isin], as_of)[isin]
    assert abs(value) < 0.01, (
        f"momentum picked up a spike inside the 1-month skip window (got {value:.3f})"
    )


def test_momentum_returns_nan_on_insufficient_history(db: Database):
    db.upsert_df("instruments", make_instruments(1), ["isin"])
    isin = "INE000000000"
    db.upsert_df(
        "prices_daily",
        make_prices([isin], dt.date(2023, 1, 1), dt.date(2023, 3, 1)),
        ["isin", "date"],
    )
    assert pd.isna(Momentum12_1().compute(db, [isin], dt.date(2023, 3, 1))[isin])


def test_momentum_detects_a_real_trend(db: Database):
    """Two stocks, one rising and one falling, must rank in that order."""
    instruments = make_instruments(2)
    db.upsert_df("instruments", instruments, ["isin"])
    up, down = "INE000000000", "INE000000001"

    as_of = dt.date(2023, 6, 30)
    frames = []
    for isin, daily in ((up, 1.0015), (down, 0.9985)):
        df = make_prices([isin], dt.date(2022, 1, 1), as_of, seed=11)
        df = df.sort_values("date").reset_index(drop=True)
        df["adj_close"] = 100.0 * (daily ** np.arange(len(df)))
        df["close"] = df["adj_close"]
        frames.append(df)
    db.upsert_df("prices_daily", pd.concat(frames), ["isin", "date"])

    values = Momentum12_1().compute(db, [up, down], as_of)
    assert values[up] > 0 > values[down]
