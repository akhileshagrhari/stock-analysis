"""Red-flag overlay — DESIGN §6.2.

These do not adjust the score, they **cap** it: a company tripping any of them
cannot be rated BUY regardless of how good its factors look. The justification in
DESIGN is that these signals are cheap to compute, historically informative in
Indian markets, and *not* well captured by a smooth linear factor score — most
Indian mid-cap blowups were visible in this list before the price moved.

THE TRI-STATE
-------------
Every flag returns one of three states, never two:

    TRIPPED   the condition is met, on evidence
    CLEAR     the condition was evaluated and is not met
    UNKNOWN   the data required to evaluate it is not present

The distinction between CLEAR and UNKNOWN is the entire reason this module is
not a set of boolean columns. Two of the six flags are UNKNOWN for every company
in the system right now — promoter pledge, because `NSE.shareholding()` carries
no pledged-shares figure, and credit rating, because nothing ingests ratings at
all. Collapsing those to False would turn the most informative red flag in the
Indian mid-cap universe into a clean bill of health for precisely the companies
it exists to catch.

So an unflagged company is not certified; it is unflagged *on the flags that
could be checked*, and the signal carries the list of the ones that could not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from stockanalysis.factors.panel import Panel
from stockanalysis.factors.quality import cfo_to_pat_history


class FlagState(StrEnum):
    TRIPPED = "TRIPPED"
    CLEAR = "CLEAR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FlagDefinition:
    name: str
    description: str
    # Set where no ingest path supplies the data at all, as opposed to a
    # company-by-company gap. Surfaced by `unreachable_flags()` so the shortfall
    # is a documented limitation rather than a silent one.
    reachable: bool = True


PLEDGE_THRESHOLD_PCT = 25.0
CFO_PAT_FLOOR = 0.5
CFO_PAT_YEARS = 3
PROMOTER_DECLINE_QUARTERS = 3
CONTINGENT_TO_NETWORTH = 0.5

DEFINITIONS: list[FlagDefinition] = [
    FlagDefinition(
        "promoter_pledge",
        f"Promoter pledge > {PLEDGE_THRESHOLD_PCT:.0f}% of promoter holding",
        reachable=False,   # NSE.shareholding() has no pledged-shares figure
    ),
    FlagDefinition(
        "auditor_qualification",
        "Auditor qualification, adverse opinion or disclaimer",
    ),
    FlagDefinition(
        "weak_cash_conversion",
        f"CFO/PAT < {CFO_PAT_FLOOR} for {CFO_PAT_YEARS} consecutive years",
    ),
    FlagDefinition(
        "promoter_selling",
        f"Promoter holding falling {PROMOTER_DECLINE_QUARTERS} consecutive quarters",
    ),
    FlagDefinition(
        "contingent_liabilities",
        f"Contingent liabilities > {CONTINGENT_TO_NETWORTH:.0%} of net worth",
    ),
    FlagDefinition(
        "rating_downgrade",
        "Any credit rating downgrade",
        reachable=False,   # no ratings source is ingested
    ),
]

FLAG_NAMES = [d.name for d in DEFINITIONS]


def unreachable_flags() -> list[str]:
    """Flags no data source in the system can currently evaluate."""
    return [d.name for d in DEFINITIONS if not d.reachable]


def evaluate(panel: Panel) -> pd.DataFrame:
    """One row per ISIN, one column per flag, each holding a FlagState value."""
    out = pd.DataFrame(
        FlagState.UNKNOWN.value, index=panel.isins, columns=FLAG_NAMES, dtype=object
    )

    out["promoter_pledge"] = _promoter_pledge(panel)
    out["auditor_qualification"] = _auditor(panel)
    out["weak_cash_conversion"] = _weak_cash_conversion(panel)
    out["promoter_selling"] = _promoter_selling(panel)
    out["contingent_liabilities"] = _contingent_liabilities(panel)
    # rating_downgrade stays UNKNOWN throughout — no source.

    return out


def summarise(flags: pd.DataFrame) -> pd.DataFrame:
    """Per-ISIN `tripped` / `unknown` lists and a `has_red_flag` boolean."""
    tripped, unknown = [], []
    for _isin, row in flags.iterrows():
        tripped.append([c for c in flags.columns if row[c] == FlagState.TRIPPED.value])
        unknown.append([c for c in flags.columns if row[c] == FlagState.UNKNOWN.value])

    return pd.DataFrame(
        {
            "tripped": tripped,
            "unknown": unknown,
            "has_red_flag": [len(t) > 0 for t in tripped],
        },
        index=flags.index,
    )


# ----------------------------------------------------------------------
# Individual flags. Each returns a Series of FlagState values over panel.isins.
# ----------------------------------------------------------------------


def _states(condition: pd.Series, evaluable: pd.Series, index: list[str]) -> pd.Series:
    """Fold a condition and an "was it checkable" mask into the tri-state."""
    out = pd.Series(FlagState.UNKNOWN.value, index=index, dtype=object)
    ok = evaluable.reindex(index).fillna(False).astype(bool)
    cond = condition.reindex(index).fillna(False).astype(bool)
    out[ok] = np.where(cond[ok], FlagState.TRIPPED.value, FlagState.CLEAR.value)
    return out


def _promoter_pledge(panel: Panel) -> pd.Series:
    """Pledge above the threshold.

    Reads `promoter_pledged_pct`, which is NULL for every row NSE supplies. The
    check is written out in full anyway so that the day a pledge source is
    ingested this starts working without anyone having to remember it existed.
    """
    if panel.shareholding.empty:
        return pd.Series(FlagState.UNKNOWN.value, index=panel.isins, dtype=object)

    latest = panel.shareholding.groupby("isin", as_index=False).head(1).set_index("isin")
    pledge = pd.to_numeric(
        latest.get("promoter_pledged_pct", pd.Series(dtype=float)), errors="coerce"
    )
    return _states(pledge > PLEDGE_THRESHOLD_PCT, pledge.notna(), panel.isins)


def _auditor(panel: Panel) -> pd.Series:
    """Anything other than an unmodified opinion.

    NOT_STATED counts as UNKNOWN rather than CLEAR — it means the extractor
    could not find the opinion, not that the auditor signed off.
    """
    if panel.latest.empty or "auditor_opinion" not in panel.latest.columns:
        return pd.Series(FlagState.UNKNOWN.value, index=panel.isins, dtype=object)

    opinion = panel.latest["auditor_opinion"].reindex(panel.isins)
    evaluable = opinion.notna() & (opinion != "NOT_STATED")
    tripped = opinion.isin(["QUALIFIED", "ADVERSE", "DISCLAIMER"])
    return _states(tripped, evaluable, panel.isins)


def _weak_cash_conversion(panel: Panel) -> pd.Series:
    """CFO/PAT below the floor in each of the last three years.

    Requires three consecutive years of *computable* ratios. Two years plus a
    gap is UNKNOWN, not CLEAR: the flag is specifically about persistence, and
    a company with one missing cash flow statement has not demonstrated the
    absence of a pattern.
    """
    history = cfo_to_pat_history(panel)
    if history.empty:
        return pd.Series(FlagState.UNKNOWN.value, index=panel.isins, dtype=object)

    condition, evaluable = {}, {}
    for isin, grp in history.groupby("isin"):
        grp = grp.sort_values("period_end_date", ascending=False).head(CFO_PAT_YEARS)
        ratios = grp["cfo_to_pat"]
        enough = len(ratios) >= CFO_PAT_YEARS and ratios.notna().all()
        evaluable[isin] = enough
        condition[isin] = bool(enough and (ratios < CFO_PAT_FLOOR).all())

    return _states(
        pd.Series(condition, dtype=object).astype(bool),
        pd.Series(evaluable, dtype=object).astype(bool),
        panel.isins,
    )


def _promoter_selling(panel: Panel) -> pd.Series:
    """Promoter holding strictly lower in each of three successive quarters.

    Needs four observations to see three declines. Strict inequality: a holding
    flat to two decimal places is not selling, and treating it as such would
    flag a large part of the universe on rounding.
    """
    if panel.shareholding.empty:
        return pd.Series(FlagState.UNKNOWN.value, index=panel.isins, dtype=object)

    needed = PROMOTER_DECLINE_QUARTERS + 1
    condition, evaluable = {}, {}
    for isin, grp in panel.shareholding.groupby("isin"):
        grp = grp.sort_values("quarter_end", ascending=False)
        holdings = pd.to_numeric(grp["promoter_pct"], errors="coerce").dropna()
        enough = len(holdings) >= needed
        evaluable[isin] = enough
        if not enough:
            condition[isin] = False
            continue
        recent = holdings.head(needed).tolist()   # newest first
        condition[isin] = all(
            recent[i] < recent[i + 1] for i in range(PROMOTER_DECLINE_QUARTERS)
        )

    return _states(
        pd.Series(condition, dtype=object).astype(bool),
        pd.Series(evaluable, dtype=object).astype(bool),
        panel.isins,
    )


def _contingent_liabilities(panel: Panel) -> pd.Series:
    """Contingent liabilities above half of net worth.

    Negative net worth is TRIPPED rather than UNKNOWN whenever any contingent
    liability is disclosed: a company with no equity to absorb them has failed
    this test in the way the test is meant to detect, and NaN would let it
    through.
    """
    contingent = panel.col("contingent_liabilities")
    equity = panel.col("total_equity")

    evaluable = contingent.notna() & equity.notna()
    tripped = ((equity <= 0) & (contingent > 0)) | (
        contingent > CONTINGENT_TO_NETWORTH * equity
    )
    return _states(tripped, evaluable, panel.isins)
