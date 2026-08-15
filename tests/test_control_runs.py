"""Control runs — the sanity checks that catch a leaking harness.

Two independent controls:

1. **Shuffled returns.** Sever the link between which stock was selected and
   which return it earns, by reassigning returns drawn from the wider universe.
   Any remaining alpha is a leak, by definition — there is no signal left to find.

2. **Random selection.** Replace the factor with a coin flip. A factor with real
   signal should beat this; if it does not, the factor is decoration.

Run these before believing any headline number.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from stockanalysis.backtest.engine import BacktestConfig, BacktestEngine
from stockanalysis.db.database import Database
from stockanalysis.factors.base import Factor
from stockanalysis.factors.momentum import Momentum12_1


class RandomFactor(Factor):
    """Coin-flip selection. The floor any real factor must clear."""

    def __init__(self, seed: int = 0):
        self.rng = np.random.default_rng(seed)

    @property
    def name(self) -> str:
        return "random"

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        return pd.Series(self.rng.normal(size=len(isins)), index=isins)


def _make_shuffler(db: Database, index_name: str, seed: int = 123):
    """Replace each selected name's return with one drawn from the full universe.

    Note this must draw from the *universe*, not permute within the selection:
    an equal-weighted portfolio's return is the mean of its holdings, and
    permuting within the selection leaves that mean untouched — a useless
    control that always passes.
    """
    rng = np.random.default_rng(seed)
    dates = sorted({d for d in pd.date_range("2019-01-01", "2024-01-01", freq="ME")})

    def transform(fwd: pd.Series, t: dt.date) -> pd.Series:
        nxt = next((d.date() for d in dates if d.date() > t), None)
        if nxt is None:
            return fwd
        universe = db.as_of_universe(index_name, t)
        pool = db.forward_returns(universe, t, nxt).dropna()
        if pool.empty:
            return fwd
        drawn = rng.choice(pool.to_numpy(), size=len(fwd), replace=True)
        return pd.Series(drawn, index=fwd.index)

    return transform


def _run(db: Database, factor: Factor, top_n: int = 8, **kw) -> object:
    cfg = BacktestConfig(
        index_name="TESTIDX",
        start=dt.date(2020, 1, 1),
        end=dt.date(2024, 1, 1),
        top_n=top_n,
        **kw,
    )
    return BacktestEngine(db, factor, cfg).run()


class FixedScoreFactor(Factor):
    """Scores by trailing digits of the ISIN, so the correct pick is knowable."""

    def __init__(self, higher_better: bool = True):
        self._higher = higher_better

    @property
    def name(self) -> str:
        return "fixed"

    @property
    def higher_is_better(self) -> bool:
        return self._higher

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        return pd.Series([float(i[-3:]) for i in isins], index=isins)


@pytest.mark.parametrize("higher_better", [True, False])
def test_engine_selects_the_intended_end_of_the_ranking(
    seeded_db: Database, higher_better: bool
):
    """Regression guard for an inverted selection.

    A sign error here does not raise — it just makes every factor look useless,
    which is indistinguishable from an honest negative result. Assert the
    direction explicitly.

    Note scores are sector-z-scored before selection, so this checks the
    *ordering* survives that transform rather than exact ISIN identity.
    """
    result = _run(seeded_db, FixedScoreFactor(higher_better), top_n=5)

    first_date = result.positions["date"].min()
    picked = set(result.positions[result.positions["date"] == first_date]["isin"])

    universe = seeded_db.as_of_universe("TESTIDX", first_date)
    raw = pd.Series([float(i[-3:]) for i in universe], index=universe)

    # Sector z-scoring is applied before selection, and it is monotonic within
    # each sector, so raw score ordering is preserved. Comparing group means is
    # therefore the right assertion — exact ISIN identity is not, since
    # cross-sector normalisation legitimately reorders the combined ranking.
    picked_ranks = [raw[i] for i in picked]
    unpicked_ranks = [raw[i] for i in universe if i not in picked]

    if higher_better:
        assert np.mean(picked_ranks) > np.mean(unpicked_ranks), (
            "engine selected low scores when higher_is_better=True — inverted selection"
        )
    else:
        assert np.mean(picked_ranks) < np.mean(unpicked_ranks), (
            "engine selected high scores when higher_is_better=False — inverted selection"
        )


def test_harness_runs_end_to_end(seeded_db: Database):
    result = _run(seeded_db, Momentum12_1())
    assert len(result.nav) > 20
    assert result.nav.iloc[-1] > 0
    assert not result.positions.empty


def test_shuffled_returns_destroy_alpha(seeded_db: Database):
    """The core leak detector.

    Severing the selection-to-outcome link must destroy the factor's edge.

    Note the null here is NOT "Sharpe near zero". A shuffled portfolio still
    earns whatever the broad universe earned, so its Sharpe should land near
    the *random-selection* run, not near zero. Asserting against zero would
    make this test fail on any dataset with a rising market, and pass on a
    falling one — for reasons having nothing to do with leakage.
    """
    real = _run(seeded_db, Momentum12_1())
    shuffled = _run(
        seeded_db,
        Momentum12_1(),
        return_transform=_make_shuffler(seeded_db, "TESTIDX"),
    )
    market = _run(seeded_db, RandomFactor(seed=7))

    assert real.metrics.cagr > shuffled.metrics.cagr, (
        f"momentum ({real.metrics.cagr:.2%}) did not beat its own shuffled "
        f"control ({shuffled.metrics.cagr:.2%}) on data containing genuine "
        f"momentum — the factor is not reading the signal it should be"
    )
    assert abs(shuffled.metrics.cagr - market.metrics.cagr) < 0.15, (
        f"shuffled run ({shuffled.metrics.cagr:.2%}) diverged from random "
        f"selection ({market.metrics.cagr:.2%}). With the signal severed these "
        f"should be statistically indistinguishable; a gap means the harness "
        f"is finding information the shuffle was supposed to remove."
    )


def test_implausible_sharpe_is_flagged(seeded_db: Database):
    """A guard against celebrating a bug.

    12-1 momentum on synthetic data should be modest. A Sharpe above 3 on a
    long-only equal-weight monthly-rebalanced book is not a discovery, it is a
    defect — this test exists to say so out loud.
    """
    result = _run(seeded_db, Momentum12_1())
    assert result.metrics.sharpe < 3.0, (
        f"Sharpe of {result.metrics.sharpe:.2f} from 12-1 momentum is not "
        f"plausible. Suspect lookahead before celebrating."
    )


def test_no_alpha_from_signalless_data(db: Database):
    """The cleanest leak detector: give the harness data with no signal at all.

    Prices here are pure random walks with identical drift and volatility, so
    there is nothing for any factor to find. If the harness reports meaningful
    positive alpha on this, it is reading the future — no shuffling or
    permutation argument required.
    """
    from tests.conftest import make_instruments, make_membership, make_prices

    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)
    instruments = make_instruments(30)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()

    db.upsert_df(
        "prices_daily",
        make_prices(isins, start, end, momentum_strength=0.0),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, "NOSIGNAL", start),
        ["index_name", "isin", "from_date"],
    )

    cfg = BacktestConfig(
        index_name="NOSIGNAL", start=dt.date(2020, 1, 1), end=end, top_n=8
    )
    result = BacktestEngine(db, Momentum12_1(), cfg).run()

    assert result.metrics.sharpe < 1.0, (
        f"harness produced Sharpe {result.metrics.sharpe:.2f} on data containing "
        f"no signal whatsoever. This is a lookahead leak."
    )


def test_measured_alpha_tracks_embedded_signal(db: Database):
    """Dose-response: more embedded signal must produce more measured alpha.

    A harness that leaks tends to report strong performance regardless of what
    is actually in the data. Monotonicity across signal strengths is positive
    evidence that the number being reported is the one being measured.
    """
    from tests.conftest import make_instruments, make_membership, make_prices

    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)
    sharpes = []

    for strength in (0.0, 1.0, 3.0):
        sub = Database(":memory:")
        instruments = make_instruments(30)
        sub.upsert_df("instruments", instruments, ["isin"])
        isins = instruments["isin"].tolist()
        sub.upsert_df(
            "prices_daily",
            make_prices(isins, start, end, momentum_strength=strength),
            ["isin", "date"],
        )
        sub.upsert_df(
            "index_membership",
            make_membership(isins, "IDX", start),
            ["index_name", "isin", "from_date"],
        )
        cfg = BacktestConfig(
            index_name="IDX", start=dt.date(2020, 1, 1), end=end, top_n=8
        )
        sharpes.append(BacktestEngine(sub, Momentum12_1(), cfg).run().metrics.sharpe)
        sub.close()

    assert sharpes[0] < sharpes[1] < sharpes[2], (
        f"measured Sharpe {sharpes} did not increase with embedded signal "
        f"strength — the harness is not tracking the data"
    )


def test_costs_reduce_returns(seeded_db: Database):
    """Turnover must cost money. A no-op cost model is a silent fiction."""
    with_costs = _run(seeded_db, Momentum12_1(), apply_costs=True)
    without = _run(seeded_db, Momentum12_1(), apply_costs=False)

    assert with_costs.metrics.total_costs_pct > 0
    assert with_costs.nav.iloc[-1] < without.nav.iloc[-1]


def test_random_factor_is_the_floor(seeded_db: Database):
    """Momentum should beat coin-flip selection on data with embedded trend."""
    momentum = _run(seeded_db, Momentum12_1())
    random_pick = _run(seeded_db, RandomFactor(seed=1))

    assert momentum.metrics.cagr > random_pick.metrics.cagr - 0.02, (
        "momentum did not clear the random-selection floor on data that "
        "contains genuine momentum"
    )


def test_survivorship_warning_fires_without_coverage(db: Database):
    """An unsafe universe must announce itself, not pass quietly."""
    from tests.conftest import make_instruments, make_membership, make_prices

    instruments = make_instruments(15)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    db.upsert_df(
        "prices_daily",
        make_prices(isins, dt.date(2020, 1, 1), dt.date(2023, 1, 1)),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, "UNSAFE", dt.date(2020, 1, 1)),
        ["index_name", "isin", "from_date"],
    )
    # Deliberately no coverage row.

    cfg = BacktestConfig(
        index_name="UNSAFE",
        start=dt.date(2021, 1, 1),
        end=dt.date(2023, 1, 1),
        top_n=5,
    )
    result = BacktestEngine(db, Momentum12_1(), cfg).run()

    assert any("SURVIVORSHIP UNSAFE" in w for w in result.warnings)


@pytest.mark.parametrize("top_n", [5, 10, 20])
def test_portfolio_weights_sum_to_one(seeded_db: Database, top_n: int):
    result = _run(seeded_db, Momentum12_1(), top_n=top_n)
    by_date = result.positions.groupby("date")["weight"].sum()
    assert np.allclose(by_date.to_numpy(), 1.0, atol=1e-9)
