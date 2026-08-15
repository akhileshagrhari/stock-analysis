"""Quality factors — DESIGN §6.1, 30% of the composite and the largest weight.

Two of these are not the textbook set. DESIGN singles out **CFO/PAT** and the
**accruals ratio** as carrying disproportionate weight, on the grounds that
persistent operating cash flow far below reported profit is the single most
reliable published warning sign in Indian mid-caps. That claim is the reason
this family is weighted highest, so the two of them get 40% of the family
between them rather than an equal share alongside ROE and debt/equity.

Both depend on the cash flow statement, which exists only in the annual report
PDF. Until the phase-1 backfill runs they are uncomputable, and the composite
reports that as missing weight rather than as a passing grade.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stockanalysis.factors.base import PanelFactor, safe_divide
from stockanalysis.factors.panel import Panel
from stockanalysis.factors.value import ebit

FAMILY = "quality"

# Interest coverage is unbounded above — a debt-free company divides by ~zero.
# Capped rather than winsorised, because the distinction between 40x and 400x
# covered is not information, it is just an absence of debt.
MAX_INTEREST_COVERAGE = 50.0


class Roe(PanelFactor):
    """PAT / total equity.

    `safe_divide` refuses a negative denominator here, which matters more than
    it looks: a loss-making company with negative net worth produces a
    *positive* ROE, and would otherwise rank near the top of the quality factor
    on the strength of being nearly insolvent.
    """

    name = "roe"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(panel.col("pat"), panel.col("total_equity"))


class Roce(PanelFactor):
    """EBIT / capital employed, where capital employed is equity + total debt.

    The pre-tax, pre-leverage counterpart to ROE. It is the more honest measure
    of the business when comparing across companies with different capital
    structures, which is most of the point of having both.
    """

    name = "roce"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        capital = panel.col("total_equity") + panel.col("total_debt").fillna(0.0)
        return safe_divide(ebit(panel), capital)


class DebtToEquity(PanelFactor):
    name = "debt_to_equity"
    family = FAMILY
    higher_is_better = False

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(panel.col("total_debt"), panel.col("total_equity"))


class InterestCoverage(PanelFactor):
    """EBIT / finance costs, capped.

    A company with no borrowings has no meaningful coverage ratio rather than an
    infinite one, so zero interest maps to the cap — the top of the ranking,
    which is where it belongs — instead of to NaN, which would drop genuinely
    debt-free companies out of the family.
    """

    name = "interest_coverage"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        interest = panel.col("interest_expense")
        e = ebit(panel)
        covered = safe_divide(e, interest).clip(upper=MAX_INTEREST_COVERAGE)
        debt_free = (interest.fillna(0.0) <= 0) & e.notna() & (e > 0)
        return covered.mask(debt_free, MAX_INTEREST_COVERAGE)


class CfoToPat(PanelFactor):
    """Operating cash flow / profit after tax. DESIGN's headline warning sign.

    A company reporting profits it never collects in cash shows a ratio
    persistently below 1. Sustained below 0.5 it also trips a red flag; as a
    factor it contributes continuously rather than only at the threshold.

    Only computed on positive PAT. On a loss the ratio inverts its meaning —
    negative OCF over negative PAT comes out positive and large, which would
    read as excellent cash conversion.
    """

    name = "cfo_to_pat"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(panel.col("ocf"), panel.col("pat"))


class Accruals(PanelFactor):
    """(PAT - OCF) / total assets. Lower is better.

    The balance-sheet-scaled version of the same idea as CFO/PAT, and it keeps
    working where that one does not: the numerator is a difference rather than a
    ratio, so a loss-making company still gets a meaningful reading. The two
    disagree in exactly the cases worth looking at by hand.
    """

    name = "accruals"
    family = FAMILY
    higher_is_better = False

    def from_panel(self, panel: Panel) -> pd.Series:
        gap = panel.col("pat") - panel.col("ocf")
        return safe_divide(gap, panel.col("total_assets"))


# DESIGN §6.1's "two India-specific earnings-quality checks that carry
# disproportionate weight" — stated here as numbers rather than left implicit in
# the choice of which factors to include.
INTRA_FAMILY_WEIGHTS: dict[str, float] = {
    "roe": 0.20,
    "roce": 0.20,
    "debt_to_equity": 0.10,
    "interest_coverage": 0.10,
    "cfo_to_pat": 0.25,
    "accruals": 0.15,
}


def cfo_to_pat_history(panel: Panel) -> pd.DataFrame:
    """CFO/PAT per year per ISIN, newest first. Feeds the sustained-weakness flag.

    Returned as a frame rather than a series because the red flag needs three
    consecutive years, and "consecutive" is a property of the sequence that a
    single aggregate cannot express.
    """
    if panel.annual.empty:
        return pd.DataFrame(columns=["isin", "period_end_date", "cfo_to_pat"])

    df = panel.annual[["isin", "period_end_date", "ocf", "pat"]].copy()
    df["cfo_to_pat"] = safe_divide(df["ocf"], df["pat"])
    return df.replace([np.inf, -np.inf], np.nan)


ALL: list[PanelFactor] = [
    Roe(),
    Roce(),
    DebtToEquity(),
    InterestCoverage(),
    CfoToPat(),
    Accruals(),
]
