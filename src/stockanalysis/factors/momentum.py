"""12-1 momentum.

The classic cross-sectional momentum specification: total return from t-12
months to t-1 month, skipping the most recent month to sidestep short-term
reversal.

This factor exists in phase 0 to validate the *harness*, not to make money. It
is well documented, weakly predictive, and — critically — has a known plausible
magnitude. If the backtest reports a Sharpe of 4 on this, the harness is broken.
That negative result is the entire point of building it first.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.factors.base import Factor

FAMILY = "momentum"


class Momentum12_1(Factor):
    def __init__(
        self,
        lookback_months: int = 12,
        skip_months: int = 1,
        min_observations: int = 180,
    ):
        self.lookback_months = lookback_months
        self.skip_months = skip_months
        self.min_observations = min_observations

    @property
    def name(self) -> str:
        return f"momentum_{self.lookback_months}_{self.skip_months}"

    @property
    def family(self) -> str:
        return FAMILY

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        if not isins:
            return pd.Series(dtype=float)

        lookback_days = int(self.lookback_months * 30.44) + 45
        prices = db.as_of_prices(isins, as_of, lookback_days=lookback_days)
        if prices.empty:
            return pd.Series(index=isins, dtype=float)

        prices["date"] = pd.to_datetime(prices["date"])
        start_target = pd.Timestamp(as_of) - pd.DateOffset(months=self.lookback_months)
        end_target = pd.Timestamp(as_of) - pd.DateOffset(months=self.skip_months)

        out: dict[str, float] = {}
        for isin, grp in prices.groupby("isin"):
            grp = grp.sort_values("date")
            if len(grp) < self.min_observations:
                out[isin] = np.nan
                continue

            start_px = _price_asof(grp, start_target)
            end_px = _price_asof(grp, end_target)
            if start_px is None or end_px is None or start_px <= 0:
                out[isin] = np.nan
                continue

            out[isin] = (end_px / start_px) - 1.0

        return pd.Series(out).reindex(isins)


class PriceTo200DMA(Factor):
    """Close divided by its 200-day moving average.

    A trend-participation measure rather than a return measure, and it is not
    redundant with 12-1 momentum: a stock that rose hard and has since rolled
    over still scores well on 12-1 while sitting below its 200-DMA. Where the
    two disagree is exactly where momentum strategies take their losses.
    """

    def __init__(self, window: int = 200, min_observations: int = 150):
        self.window = window
        self.min_observations = min_observations

    @property
    def name(self) -> str:
        return f"price_to_{self.window}dma"

    @property
    def family(self) -> str:
        return FAMILY

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        if not isins:
            return pd.Series(dtype=float)
        prices = db.as_of_prices(isins, as_of, lookback_days=int(self.window * 1.6))
        if prices.empty:
            return pd.Series(index=isins, dtype=float)

        out: dict[str, float] = {}
        for isin, grp in prices.groupby("isin"):
            px = grp.sort_values("date")["adj_close"].dropna()
            px = px[px > 0]
            if len(px) < self.min_observations:
                continue
            dma = float(px.tail(self.window).mean())
            if dma > 0:
                out[isin] = float(px.iloc[-1]) / dma

        return pd.Series(out, dtype=float).reindex(isins)


class RelativeStrength(Factor):
    """6-month return in excess of the equal-weighted universe's.

    DESIGN specifies relative strength against the Nifty 500. No index price
    series is ingested — the universe table holds constituents, not the index
    level — so the equal-weighted universe stands in for it. That is a real
    substitution and worth knowing about: it makes this factor market-neutral by
    construction, whereas a true index-relative measure would carry whatever
    difference exists between the Nifty 100 and the Nifty 500.

    Also note that cross-sectional z-scoring already removes the universe mean,
    so this is partly collinear with plain 6-month momentum. It earns its slot
    only through the different horizon to the 12-1 factor, and the attribution
    report is where that gets checked rather than assumed.
    """

    def __init__(self, lookback_months: int = 6, min_observations: int = 90):
        self.lookback_months = lookback_months
        self.min_observations = min_observations

    @property
    def name(self) -> str:
        return f"relative_strength_{self.lookback_months}m"

    @property
    def family(self) -> str:
        return FAMILY

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        if not isins:
            return pd.Series(dtype=float)
        lookback_days = int(self.lookback_months * 30.44) + 30
        prices = db.as_of_prices(isins, as_of, lookback_days=lookback_days)
        if prices.empty:
            return pd.Series(index=isins, dtype=float)

        prices["date"] = pd.to_datetime(prices["date"])
        start_target = pd.Timestamp(as_of) - pd.DateOffset(months=self.lookback_months)

        out: dict[str, float] = {}
        for isin, grp in prices.groupby("isin"):
            grp = grp.sort_values("date")
            if len(grp) < self.min_observations:
                continue
            start_px = _price_asof(grp, start_target)
            end_px = float(grp["adj_close"].iloc[-1])
            if start_px is None or end_px <= 0:
                continue
            out[isin] = (end_px / start_px) - 1.0

        returns = pd.Series(out, dtype=float)
        if returns.empty:
            return pd.Series(index=isins, dtype=float)
        # Median, not mean: one name that tripled would otherwise drag the
        # benchmark up and mark the whole universe down against it.
        return (returns - returns.median()).reindex(isins)


ALL: list[Factor] = [Momentum12_1(), PriceTo200DMA(), RelativeStrength()]


def _price_asof(grp: pd.DataFrame, target: pd.Timestamp) -> float | None:
    """Last adjusted close at or before `target`, within a 15-day tolerance.

    Indian markets close for a lot of holidays; an exact-date lookup drops
    perfectly good observations. The tolerance stops us silently reaching back
    months when a stock simply was not trading.
    """
    eligible = grp[grp["date"] <= target]
    if eligible.empty:
        return None
    row = eligible.iloc[-1]
    if (target - row["date"]).days > 15:
        return None
    px = row["adj_close"]
    return float(px) if pd.notna(px) and px > 0 else None
