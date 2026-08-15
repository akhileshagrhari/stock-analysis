"""Value factors — DESIGN §6.1, 25% of the composite.

Every ratio here is expressed **yield-side up** — E/P rather than P/E, book-to-
price rather than price-to-book. That is not cosmetic. P/E is a discontinuous
function of earnings: as EPS falls through zero it runs to +infinity, flips sign,
and returns from -infinity, so a company on the edge of a loss ranks somewhere
arbitrary and a winsoriser cannot help. E/P passes smoothly through zero and
orders the universe the way an investor actually would. The same argument
applies to each of the others, and it means every factor in this file is
higher-is-better, which removes a whole class of sign error.
"""

from __future__ import annotations

import pandas as pd

from stockanalysis.factors.base import PanelFactor, safe_divide
from stockanalysis.factors.panel import Panel

FAMILY = "value"


class EarningsYield(PanelFactor):
    """EPS / price. The inverse of P/E, and the cleanest ratio available here.

    Notably it needs no share count: both terms are already per-share, so this
    is the one value factor that survives when the implied-share-count route
    fails.
    """

    name = "earnings_yield"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(panel.col("eps"), panel.price)


class BookToPrice(PanelFactor):
    """Total equity / market cap. The inverse of P/B.

    Equity is the group figure including non-controlling interests, while market
    cap is implied from EPS attributable to the parent's owners. For a group with
    material minorities that overstates book-to-price. Correcting it needs the
    NCI field that PHASE1-FINDINGS §2.1 is still holding open, so the bias is
    recorded here rather than silently absorbed.
    """

    name = "book_to_price"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(panel.col("total_equity"), panel.market_cap)


class EbitdaToEv(PanelFactor):
    """EBITDA / enterprise value. The inverse of EV/EBITDA.

    EBITDA is only extracted when the report states it, because deriving it is a
    judgement call the extraction schema deliberately refuses to make. So it is
    derived *here* instead, deterministically and in the open, as
    PBT + finance costs + depreciation. That is the standard build-up; it is
    recorded as a fallback so a factor computed from a stated EBITDA and one
    computed from a reconstructed one are distinguishable later.
    """

    name = "ebitda_to_ev"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(ebitda(panel), panel.enterprise_value)


class FcfYield(PanelFactor):
    """Free cash flow / market cap.

    FCF falls back to DESIGN's own identity, OCF - capex, when the report does
    not state it — which is nearly always, since Indian annual reports rarely
    print a free cash flow line. Unlike the earnings-based factors this one is
    left to go negative: a cash-burning company genuinely belongs at the bottom
    of this ranking, and unlike a negative P/E the number stays well-behaved.
    """

    name = "fcf_yield"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        return safe_divide(free_cash_flow(panel), panel.market_cap)


class Peg(PanelFactor):
    """Earnings yield divided by PAT growth — PEG, inverted so higher is better.

    PEG is in DESIGN's list and is the weakest member of it. It is undefined for
    any company whose earnings shrank, which in a bad year is a large fraction of
    the universe, and near-zero growth sends it to infinity. Expressed this way
    round (E/P x growth) it degrades gracefully instead: zero growth gives zero,
    and the "undefined" case is restricted to genuinely negative growth.
    """

    name = "peg_inverse"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        from stockanalysis.factors.growth import pat_cagr

        ey = safe_divide(panel.col("eps"), panel.price)
        growth = pat_cagr(panel)
        return (ey * growth.where(growth > 0)).astype(float)


# ----------------------------------------------------------------------
# Shared derivations. Public because the quality family needs them too, and two
# definitions of EBITDA in one codebase is one too many.
# ----------------------------------------------------------------------


def ebit(panel: Panel) -> pd.Series:
    """Profit before tax plus finance costs."""
    return panel.col("profit_before_tax") + panel.col("interest_expense").fillna(0.0)


def ebitda(panel: Panel) -> pd.Series:
    """Stated EBITDA where the report gave one, else PBT + interest + depreciation."""
    stated = panel.col("ebitda")
    derived = ebit(panel) + panel.col("depreciation")
    return stated.fillna(derived)


def free_cash_flow(panel: Panel) -> pd.Series:
    """Stated FCF, else OCF - capex.

    Capex is extracted as a positive magnitude by contract (the cash flow
    statement prints it in brackets), so this subtracts rather than adds. Getting
    that sign wrong would turn the most capital-hungry companies into the ones
    generating the most cash.
    """
    stated = panel.col("fcf")
    derived = panel.col("ocf") - panel.col("capex").fillna(0.0)
    return stated.fillna(derived)


ALL: list[PanelFactor] = [
    EarningsYield(),
    BookToPrice(),
    EbitdaToEv(),
    FcfYield(),
    Peg(),
]
