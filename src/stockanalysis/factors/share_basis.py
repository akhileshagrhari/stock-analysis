"""One share basis for prices and per-share fundamentals — data issue D1.

A filing's EPS is on the share count of the day it was filed. The stored price
series is on the share count of the day it was fetched, because Yahoo re-bases
the whole history after every split and bonus (see `ingest.prices`). Divide one
by the other across a split and the ratio is off by the split. RELIANCE's 16 Aug
2026 earnings yield was exactly FY2024 EPS 102.90 over ₹1,310: a figure filed
in April 2024, six months before a 1:1 bonus, against a bonus-adjusted price.
The stock looked twice as cheap on earnings, and — through the share count
implied from that EPS — twice as cheap on book, which was the top stated reason
for its value score.

THE RULE
--------
A per-share figure filed on day B is restated to the basis of day P, the last
day of the company's stored price series, by every split and bonus with an
ex-date in (B, P]:

    restated EPS     = reported EPS     ÷ factor
    restated shares  = reported shares  × factor
    factor           = product of (shares after ÷ shares before)

B is the filing date rather than the period end because Ind AS 33 restates EPS
for a bonus that lands after the period but before the results are approved:
an action before the filing is already in the reported figure. P is the last
stored price rather than today because an action after it is not yet in the
prices either, and the point is that both sides agree.

IS THIS LOOKAHEAD?
------------------
It reads actions after a backtest's decision date, so it is worth being exact
about why the answer is no. The stored price series already embeds those same
actions — Yahoo adjusted it with them — so the only choice is whether the
fundamentals share its basis. A ratio of two figures on one basis does not
depend on which basis: earnings yield on 1 June 2024 comes out the same whether
both terms are stated in April-2024 shares or October-2024 shares. And market
cap, price × shares, reduces to the traded price on the decision date times the
share count after every split up to that date — nothing later survives the
cancellation. What would be lookahead is reading a *share count* published
later, and nothing here does.

Pure functions over frames, plus two small reads written in SQL that runs on
both engines. The scoring panel and the API's key metrics both go through here,
which is what makes a P/E on the site the inverse of the earnings yield behind
the score rather than a second opinion about it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stockanalysis.db.database import Database

# Keep in step with ingest.corporate_actions.SHARE_BASIS_ACTIONS — not imported
# from there so the factor layer does not depend on an ingest module.
_ACTION_TYPES = ("SPLIT", "BONUS")


def _as_date(value: object) -> dt.date | None:
    if value is None or (not isinstance(value, dt.date) and pd.isna(value)):
        return None
    return pd.Timestamp(value).date()


@dataclass(frozen=True)
class ShareBasis:
    """Each company's splits and bonuses, and the date its prices are based on."""

    # isin -> [(ex_date, shares after / shares before)], oldest first
    events: dict[str, list[tuple[dt.date, float]]] = field(default_factory=dict)
    # isin -> last stored price date; absent when the company has no prices
    price_basis: dict[str, dt.date] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Database, isins: list[str]) -> ShareBasis:
        if not isins:
            return cls()
        placeholders = ", ".join("?" for _ in isins)
        actions = db.query(
            f"""
            SELECT isin, ex_date, ratio FROM corporate_actions
            WHERE isin IN ({placeholders})
              AND action_type IN ('{_ACTION_TYPES[0]}', '{_ACTION_TYPES[1]}')
              AND ratio > 0
            ORDER BY isin, ex_date
            """,
            list(isins),
        )
        latest = db.query(
            f"SELECT isin, MAX(date) AS d FROM prices_daily "
            f"WHERE isin IN ({placeholders}) GROUP BY isin",
            list(isins),
        )
        events: dict[str, list[tuple[dt.date, float]]] = {}
        for r in actions.itertuples(index=False):
            day = _as_date(r.ex_date)
            if day is not None:
                events.setdefault(str(r.isin), []).append((day, float(r.ratio)))
        basis = {
            str(r.isin): d
            for r in latest.itertuples(index=False)
            if (d := _as_date(r.d)) is not None
        }
        return cls(events=events, price_basis=basis)

    def target(self, isin: str) -> dt.date | None:
        """The basis a figure for `isin` is restated to: its last price, or —
        for a company with no prices to agree with — every action on file."""
        if isin in self.price_basis:
            return self.price_basis[isin]
        known = self.events.get(isin)
        return known[-1][0] if known else None

    def factor(self, isin: str, filed: dt.date | None) -> float:
        """Shares on the target basis per share on `filed`'s basis.

        Below 1 when the price series is older than the filing and a split fell
        between them: then it is the filing that has to be stated back in the
        old shares the prices still use.
        """
        target = self.target(isin)
        if filed is None or target is None or filed == target:
            return 1.0
        lo, hi = sorted((filed, target))
        product = 1.0
        for ex_date, ratio in self.events.get(isin, ()):
            if lo < ex_date <= hi:
                product *= ratio
        return product if filed < target else 1.0 / product

    def factors(self, frame: pd.DataFrame, date_col: str = "filing_date") -> pd.Series:
        """`factor` per row of a frame with `isin` and `date_col` columns."""
        if frame.empty:
            return pd.Series(dtype=float, index=frame.index)
        return pd.Series(
            [
                self.factor(str(isin), _as_date(filed))
                for isin, filed in zip(frame["isin"], frame[date_col], strict=True)
            ],
            index=frame.index,
            dtype=float,
        )


def stated_shares_crore(frame: pd.DataFrame) -> pd.Series:
    """Shares outstanding as each filing states them — paid-up capital over
    face value, in crore, on the filing's own basis. NaN where either is absent."""
    if frame.empty or "paid_up_capital" not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    capital = pd.to_numeric(frame["paid_up_capital"], errors="coerce")
    face = pd.to_numeric(frame["face_value"], errors="coerce")
    return (capital / face.where(face > 0)).where(capital > 0).astype(float)


def restate(frame: pd.DataFrame, basis: ShareBasis, date_col: str = "filing_date") -> pd.DataFrame:
    """A copy of `frame` with its per-share columns on the price series' basis.

    `eps` is replaced by the restated figure — every consumer downstream reads
    one column and cannot pick up the wrong one — and the original is kept as
    `eps_reported` beside `share_factor`, so the restatement stays visible.
    `shares_crore` is the stated count on the same basis.
    """
    out = frame.copy()
    if out.empty:
        for column in ("share_factor", "eps_reported", "shares_crore"):
            out[column] = pd.Series(dtype=float)
        return out
    factor = basis.factors(out, date_col)
    out["share_factor"] = factor
    if "eps" in out.columns:
        out["eps_reported"] = pd.to_numeric(out["eps"], errors="coerce")
        out["eps"] = out["eps_reported"] / factor
    out["shares_crore"] = stated_shares_crore(out) * factor
    return out
