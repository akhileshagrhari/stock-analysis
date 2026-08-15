"""Growth factors — DESIGN §6.1, 20% of the composite.

A growth rate is a ratio of two numbers that may each be negative, which makes
it the family where naive arithmetic produces the most confident nonsense. Two
rules apply throughout:

**A CAGR needs a positive base.** Growing from -100 to -50 is a 50% "improvement"
that the compound-growth formula reports as -50%, and growing from -50 to +50 is
a division that changes sign. Both are excluded rather than ranked. The cost is
coverage on exactly the companies that turned around, which is a known blind spot
of every growth factor built this way and not one this implementation can fix.

**Annualise from what is actually there.** DESIGN specifies 3-year CAGRs, but the
phase-1 backfill is scoped to 3 years of reports, which is 3 observations and
therefore a 2-year span. Rather than return NaN for everything, the span actually
available is used and annualised, with a floor of two observations.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stockanalysis.factors.base import PanelFactor, safe_divide
from stockanalysis.factors.panel import Panel

FAMILY = "growth"

# Fewer years than this and a "growth rate" is one year-on-year change wearing a
# CAGR's clothes.
MIN_OBSERVATIONS = 2
TARGET_YEARS = 3


class RevenueCagr(PanelFactor):
    name = "revenue_cagr_3y"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return annualised_growth(panel, "revenue")


class PatCagr(PanelFactor):
    name = "pat_cagr_3y"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return pat_cagr(panel)


class QuarterlyRevenueYoY(PanelFactor):
    """Latest quarter's revenue against the same quarter a year earlier.

    Year-on-year rather than sequential, deliberately: Indian earnings are
    strongly seasonal (festive-season consumption, monsoon-linked rural demand,
    the March year-end push), so a quarter-on-quarter comparison measures the
    calendar more than the company.

    This is the one growth factor with real coverage before the annual-report
    backfill runs — `NSE.results_comparison` is free and needs no model. Note
    PHASE1-FINDINGS §3.2: its "last 5 quarters" can be a year or more stale, so
    the *knowledge date* is honest but the information may be old.
    """

    name = "quarterly_revenue_yoy"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return _quarterly_yoy(panel, "revenue")


class QuarterlyPatYoY(PanelFactor):
    name = "quarterly_pat_yoy"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return _quarterly_yoy(panel, "pat")


class MarginTrend(PanelFactor):
    """Change in PAT margin between the oldest and newest visible annual report.

    A level, not a ratio of levels, so it is signed and well behaved even when
    both margins are negative — which is why margin *trend* is stated as a
    difference here while revenue growth is stated as a CAGR.
    """

    name = "margin_trend"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        if panel.annual.empty:
            return pd.Series(np.nan, index=panel.isins, dtype=float)

        df = panel.annual.copy()
        df["margin"] = safe_divide(df["pat"], df["revenue"])

        out = {}
        for isin, grp in df.groupby("isin"):
            grp = grp.sort_values("period_end_date")
            margins = grp["margin"].dropna()
            if len(margins) < MIN_OBSERVATIONS:
                continue
            out[isin] = float(margins.iloc[-1] - margins.iloc[0])
        return pd.Series(out, dtype=float).reindex(panel.isins)


# ----------------------------------------------------------------------


def pat_cagr(panel: Panel) -> pd.Series:
    """Annualised PAT growth. Shared with the value family's PEG."""
    return annualised_growth(panel, "pat")


def annualised_growth(panel: Panel, column: str) -> pd.Series:
    """Compound annual growth in `column` over the visible annual history.

    Uses the true elapsed time between period ends rather than a count of rows,
    because a company that changed its financial year — India's 2014-15
    transition to a uniform 31 March year end left a lot of 9- and 18-month
    periods behind — otherwise has its growth annualised against the wrong
    denominator.
    """
    if panel.annual.empty or column not in panel.annual.columns:
        return pd.Series(np.nan, index=panel.isins, dtype=float)

    out = {}
    for isin, grp in panel.annual.groupby("isin"):
        grp = grp.sort_values("period_end_date").tail(TARGET_YEARS + 1)
        vals = pd.to_numeric(grp[column], errors="coerce")
        ok = grp[vals.notna()]
        if len(ok) < MIN_OBSERVATIONS:
            continue

        start_v = float(pd.to_numeric(ok[column]).iloc[0])
        end_v = float(pd.to_numeric(ok[column]).iloc[-1])
        if start_v <= 0:
            continue   # see module docstring

        years = (
            pd.Timestamp(ok["period_end_date"].iloc[-1])
            - pd.Timestamp(ok["period_end_date"].iloc[0])
        ).days / 365.25
        if years < 0.5:
            continue

        ratio = end_v / start_v
        if ratio <= 0:
            # Crossed into a loss. Growth is undefined; the fact is not lost,
            # it surfaces through the earnings-yield and quality factors.
            continue
        out[isin] = float(ratio ** (1.0 / years) - 1.0)

    return pd.Series(out, dtype=float).reindex(panel.isins)


def _quarterly_yoy(panel: Panel, column: str) -> pd.Series:
    """Latest quarter vs the quarter ending closest to one year before it."""
    if panel.quarterly.empty or column not in panel.quarterly.columns:
        return pd.Series(np.nan, index=panel.isins, dtype=float)

    df = panel.quarterly.copy()
    df["period_end_date"] = pd.to_datetime(df["period_end_date"])
    df[column] = pd.to_numeric(df[column], errors="coerce")

    out = {}
    for isin, grp in df.groupby("isin"):
        grp = grp.dropna(subset=[column]).sort_values("period_end_date")
        if len(grp) < 2:
            continue

        latest = grp.iloc[-1]
        target = latest["period_end_date"] - pd.DateOffset(years=1)
        gap = (grp["period_end_date"] - target).abs()
        # Within 45 days of the anniversary, or it is not the same quarter.
        candidates = grp[gap <= pd.Timedelta(days=45)]
        if candidates.empty:
            continue

        base = float(candidates.iloc[(candidates["period_end_date"] - target)
                                     .abs().argmin()][column])
        if base <= 0:
            continue
        out[isin] = float(latest[column] / base - 1.0)

    return pd.Series(out, dtype=float).reindex(panel.isins)


ALL: list[PanelFactor] = [
    RevenueCagr(),
    PatCagr(),
    QuarterlyRevenueYoY(),
    QuarterlyPatYoY(),
    MarginTrend(),
]
