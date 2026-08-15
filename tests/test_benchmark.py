"""Benchmark tests.

The benchmark is what makes a headline CAGR mean anything, so it needs to be
correct in its own right — a broken benchmark silently flatters or maligns every
strategy compared against it.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from stockanalysis.backtest.benchmark import equal_weight_benchmark, format_comparison
from stockanalysis.backtest.engine import rebalance_dates
from stockanalysis.backtest.metrics import compute_metrics
from stockanalysis.db.database import Database
from tests.conftest import make_instruments, make_membership, make_prices


def test_benchmark_tracks_a_known_constant_return(db: Database):
    """Every stock compounding at a fixed daily rate must produce that rate."""
    instruments = make_instruments(10)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()

    start, end = dt.date(2021, 1, 1), dt.date(2023, 1, 1)
    frames = []
    daily = 1.0004
    for isin in isins:
        df = make_prices([isin], start, end, seed=5).sort_values("date")
        df = df.reset_index(drop=True)
        df["adj_close"] = 100.0 * (daily ** np.arange(len(df)))
        df["close"] = df["adj_close"]
        frames.append(df)
    db.upsert_df("prices_daily", pd.concat(frames), ["isin", "date"])
    db.upsert_df(
        "index_membership", make_membership(isins, "IDX", start),
        ["index_name", "isin", "from_date"],
    )

    dates = rebalance_dates(dt.date(2021, 2, 1), end, "ME")
    nav, metrics = equal_weight_benchmark(db, "IDX", dates)

    assert not nav.empty
    expected_cagr = daily**252 - 1
    assert abs(metrics.cagr - expected_cagr) < 0.02, (
        f"benchmark CAGR {metrics.cagr:.2%} does not match the known "
        f"underlying rate {expected_cagr:.2%}"
    )


def test_benchmark_holds_the_whole_universe(db: Database):
    """Not a subset — the benchmark's return is the universe mean."""
    instruments = make_instruments(20)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    start, end = dt.date(2021, 1, 1), dt.date(2022, 6, 1)

    db.upsert_df("prices_daily", make_prices(isins, start, end), ["isin", "date"])
    db.upsert_df(
        "index_membership", make_membership(isins, "IDX", start),
        ["index_name", "isin", "from_date"],
    )

    t, t_next = dt.date(2021, 6, 30), dt.date(2021, 7, 31)
    nav, _ = equal_weight_benchmark(db, "IDX", [t, t_next], initial_capital=100.0)

    expected = 100.0 * (1 + db.forward_returns(isins, t, t_next).dropna().mean())
    assert np.isclose(nav.iloc[0], expected)


def test_comparison_names_the_loser_plainly():
    """A lagging strategy must be reported as lagging, not buried."""
    winner = compute_metrics(pd.Series([100.0, 110.0, 121.0, 133.0]))
    loser = compute_metrics(pd.Series([100.0, 101.0, 102.0, 103.0]))

    assert "LAGGED" in format_comparison(loser, winner, "Strat")
    assert "beat" in format_comparison(winner, loser, "Strat")


def test_benchmark_survives_empty_universe(db: Database):
    nav, metrics = equal_weight_benchmark(
        db, "NONEXISTENT", rebalance_dates(dt.date(2021, 1, 1), dt.date(2022, 1, 1))
    )
    assert nav.empty
    assert metrics.periods == 0
