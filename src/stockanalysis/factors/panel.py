"""The per-date fundamental panel: one point-in-time snapshot, shared by every factor.

Fifteen factors each issuing their own queries at each of ~55 rebalance dates is
both slow and, more importantly, fifteen separate opportunities to filter on the
wrong date column. Loading once per `as_of` through `Database.as_of_*` means
there is a single place where the knowledge-date rule is applied, and the factors
downstream cannot reach past it because they are handed a dataframe rather than a
connection.

DERIVED QUANTITIES
------------------
Two things every valuation factor needs are not in any table, and the way they
are obtained is the most important decision in this file.

**Share count.** No source in the system reports shares outstanding. yfinance
exposes a *current* figure, which is exactly the wrong thing: applying today's
share count to a 2021 balance sheet is both anachronistic and wrong on the
arithmetic, because buybacks and issuance moved it. Instead the count is implied
from the filing itself — `shares = PAT / EPS` — so both halves come from the same
document and carry the same knowledge date. It costs nothing in point-in-time
correctness.

**Market capitalisation** then falls out as `price x PAT / EPS`, in crore.

The cost of that route is that it is undefined when EPS or PAT is non-positive,
so **loss-making companies drop out of the value factors entirely** rather than
receiving a meaningless negative multiple. That is the honest treatment — a P/E
of -8 is not "cheap" — but it is a real coverage hole that shows up as missing
value-family weight, not as a neutral score.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockanalysis.db.database import Database

# Below this the implied share count is unstable: a near-zero EPS divides into a
# share count of billions and a market cap to match. Reported in rupees/share.
MIN_ABS_EPS = 0.01


@dataclass(frozen=True)
class Panel:
    """Everything the factors are allowed to see on `as_of`."""

    as_of: dt.date
    isins: list[str]

    annual: pd.DataFrame      # up to 5 years per isin, newest first
    latest: pd.DataFrame      # most recent annual row per isin, indexed by isin
    quarterly: pd.DataFrame   # up to 8 quarters per isin, newest first
    shareholding: pd.DataFrame
    sentiment: pd.DataFrame

    price: pd.Series          # last adjusted close at or before as_of
    shares: pd.Series         # implied, in absolute units
    market_cap: pd.Series     # rupees crore
    enterprise_value: pd.Series

    def col(self, name: str) -> pd.Series:
        """A column of `latest`, reindexed to the full universe as float.

        Factors index by ISIN throughout, and a company with no filed annual
        report must come back as NaN — "not computable" — rather than be absent
        from the series and silently dropped from an alignment.
        """
        if self.latest.empty or name not in self.latest.columns:
            return pd.Series(np.nan, index=self.isins, dtype=float)
        return pd.to_numeric(
            self.latest[name], errors="coerce"
        ).reindex(self.isins).astype(float)


def load_panel(db: Database, isins: list[str], as_of: dt.date) -> Panel:
    """Assemble the panel for `as_of`, reading only through the as_of_* path."""
    isins = list(isins)

    annual = db.as_of_fundamentals_history(isins, as_of, years=5)
    latest = (
        annual.groupby("isin", as_index=False).head(1).set_index("isin")
        if not annual.empty
        else pd.DataFrame()
    )

    price = _latest_price(db, isins, as_of)

    panel = Panel(
        as_of=as_of,
        isins=isins,
        annual=annual,
        latest=latest,
        quarterly=db.as_of_quarterly(isins, as_of, quarters=8),
        shareholding=db.as_of_shareholding_history(isins, as_of, quarters=6),
        sentiment=db.as_of_sentiment(isins, as_of, window_days=30),
        price=price,
        shares=pd.Series(dtype=float),
        market_cap=pd.Series(dtype=float),
        enterprise_value=pd.Series(dtype=float),
    )

    shares = _implied_shares(panel)
    market_cap = (price * shares / 1e7).reindex(isins)   # rupees -> crore
    ev = market_cap + panel.col("total_debt").fillna(0.0) - panel.col("cash").fillna(0.0)

    # Frozen dataclass; rebuild rather than mutate so the panel stays a value.
    return Panel(
        **{
            **panel.__dict__,
            "shares": shares,
            "market_cap": market_cap.where(market_cap > 0),
            "enterprise_value": ev.where(ev > 0),
        }
    )


def _latest_price(db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
    """Last adjusted close at or before `as_of`, within a short tolerance.

    The tolerance matters: a stock suspended eight months ago should not be
    valued off its last traded print as though nothing had happened.
    """
    px = db.as_of_prices(isins, as_of, lookback_days=45)
    if px.empty:
        return pd.Series(np.nan, index=isins, dtype=float)
    px = px.sort_values("date").groupby("isin").tail(1).set_index("isin")
    out = pd.to_numeric(px["adj_close"], errors="coerce").reindex(isins).astype(float)
    return out.where(out > 0)


def _implied_shares(panel: Panel) -> pd.Series:
    """PAT / EPS, in absolute share count. NaN where the ratio is meaningless.

    Both inputs come from one filing, so the result inherits that filing's
    knowledge date exactly — the property that rules out every external share
    count.

    Two caveats worth stating rather than burying. EPS is attributable to the
    parent's owners while PAT, per the extraction schema, is also attributable
    to owners — they agree, and if a future schema change makes PAT the group
    total the implied count would be inflated by the non-controlling share.
    And a company that issued stock mid-year has a weighted-average EPS
    denominator that no longer matches its closing share count.
    """
    pat = panel.col("pat") * 1e7        # crore -> rupees
    eps = panel.col("eps")
    ok = (eps.abs() >= MIN_ABS_EPS) & (eps > 0) & (pat > 0)
    return (pat / eps).where(ok)


class PanelCache:
    """One-slot memo so the fifteen factors at a rebalance load the panel once.

    Deliberately not an unbounded dict: the backtest walks dates forward and
    never revisits one, so anything beyond the current date is memory held for
    no reason. Keyed on the universe too, because two different universes on the
    same date are two different panels.
    """

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._panel: Panel | None = None

    def get(self, db: Database, isins: list[str], as_of: dt.date) -> Panel:
        # Keyed on the connection *object*, not `id(db)`. An id is only unique
        # among live objects: close one database, open another, and CPython
        # will hand back the same address — at which point a second connection
        # asking for the same universe on the same date silently receives the
        # first one's panel. Holding the reference is what makes the key mean
        # what it says, and one slot means one connection kept alive.
        key = (db, as_of, tuple(isins))
        if key != self._key or self._panel is None:
            self._panel = load_panel(db, isins, as_of)
            self._key = key
        return self._panel


# Module-level default. Factors constructed independently still share it, which
# is the point; pass an explicit cache to isolate (the tests do).
PANEL_CACHE = PanelCache()
