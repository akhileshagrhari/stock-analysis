"""Factor interface and sector-relative scoring.

A Factor computes one raw number per instrument, as of a date, using **only**
the database's point-in-time read path. That restriction is the entire contract:
a factor that reaches around it will look brilliant in backtest and lose money
in production.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.factors.panel import PANEL_CACHE, Panel, PanelCache


class Factor(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def higher_is_better(self) -> bool:
        return True

    @property
    def family(self) -> str:
        """Which of DESIGN §6.1's five families this factor belongs to.

        The composite weights families, not individual factors, so a family
        cannot be quietly overweighted by adding more factors to it.
        """
        return "other"

    @property
    def needs_sector_zscore(self) -> bool:
        """Whether the backtest engine should sector-z-score this factor's output.

        True for a raw factor. False for anything that has already been scored —
        the composite arrives pre-standardised with its red-flag overlay applied,
        and z-scoring it a second time would both undo the cross-sector
        comparability the composite establishes and strip the overlay's effect.
        """
        return True

    @abstractmethod
    def compute(
        self, db: Database, isins: list[str], as_of: dt.date
    ) -> pd.Series:
        """Raw factor value per ISIN. Index = isin. NaN where uncomputable."""


class PanelFactor(Factor):
    """A factor computed from the shared point-in-time panel.

    Subclasses implement `from_panel` and never touch the database, which is
    what makes the knowledge-date rule enforceable by inspection: there is no
    connection in scope to reach around it with.
    """

    def __init__(self, cache: PanelCache | None = None) -> None:
        self._cache = cache if cache is not None else PANEL_CACHE

    @abstractmethod
    def from_panel(self, panel: Panel) -> pd.Series: ...

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        if not isins:
            return pd.Series(dtype=float)
        out = self.from_panel(self._cache.get(db, isins, as_of))
        return out.reindex(isins).astype(float)


def safe_divide(
    numerator: pd.Series,
    denominator: pd.Series,
    require_positive_denominator: bool = True,
) -> pd.Series:
    """Element-wise ratio that returns NaN instead of nonsense.

    Financial ratios divide by quantities that are legitimately zero or negative
    — a debt-free company, a firm with negative net worth — and both produce a
    number that ranks as though it meant something. ROE on negative equity comes
    out *positive* for a loss-making company, which would place it near the top
    of the quality factor. NaN is the correct answer: not computable.
    """
    num = pd.to_numeric(numerator, errors="coerce")
    den = pd.to_numeric(denominator, errors="coerce")
    mask = (den > 0) if require_positive_denominator else (den != 0)
    return (num / den.where(mask)).replace([np.inf, -np.inf], np.nan)


def winsorize(s: pd.Series, limit: float = 3.0) -> pd.Series:
    """Clip outliers using a *robust* scale estimate (median + MAD).

    Mean/standard-deviation winsorization does not work here. A single extreme
    value inflates sigma enough that it falls inside its own clip bound and
    survives untouched — precisely the case we need to handle, since one
    misparsed EPS or bad price tick would otherwise dominate an entire sector's
    z-scores.

    MAD is unaffected by a small number of extreme values, so the clip bounds
    stay where the bulk of the data actually is. The 1.4826 factor makes MAD a
    consistent estimator of sigma for normally distributed data, so `limit`
    keeps its usual "number of standard deviations" meaning.
    """
    clean = s.dropna()
    if clean.empty:
        return s

    median = clean.median()
    mad = (clean - median).abs().median()

    if mad > 0:
        scale = 1.4826 * mad
    else:
        # More than half the values are identical — MAD collapses to zero and
        # tells us nothing. Fall back to standard deviation.
        scale = clean.std()
        if not np.isfinite(scale) or scale == 0:
            return s

    return s.clip(median - limit * scale, median + limit * scale)


def sector_zscore(
    values: pd.Series,
    sectors: pd.Series,
    min_sector_size: int = 5,
    winsor_limit: float = 3.0,
) -> pd.Series:
    """Z-score within sector.

    A P/E of 25 is expensive for a PSU bank and cheap for an FMCG name, so
    absolute cross-sector comparison is close to meaningless. Sectors with fewer
    than `min_sector_size` members fall back to the whole-universe distribution,
    because a z-score over three observations is noise wearing a lab coat.

    MISSING INPUT MUST STAY MISSING. A degenerate sector — every value identical,
    or every value absent — has no usable scale, and the sensible score for the
    companies that *do* have a value there is 0.0, the sector's own middle. But
    that 0.0 must not be handed to companies with no value at all. The two are
    different claims: "average for its sector" versus "not computable". The
    earlier version assigned 0.0 across the whole sector unconditionally, which
    was invisible while momentum was the only factor and always had data. Applied
    to the phase-2 composite it gave every company a neutral score on all five
    families before a single annual report had been extracted, and reported 92%
    data coverage on an empty fundamentals table.
    """
    out = pd.Series(index=values.index, dtype=float)
    aligned = sectors.reindex(values.index)
    present = values.notna()

    universe_vals = winsorize(values.dropna(), winsor_limit)
    u_mu = universe_vals.mean()
    u_sigma = universe_vals.std()

    for _sector, idx in aligned.groupby(aligned).groups.items():
        members = list(idx)
        grp = values.loc[members].dropna()
        if len(grp) >= min_sector_size:
            w = winsorize(grp, winsor_limit)
            mu, sigma = w.mean(), w.std()
        else:
            mu, sigma = u_mu, u_sigma
        if not np.isfinite(sigma) or sigma == 0:
            out.loc[members] = present.loc[members].map({True: 0.0, False: np.nan})
        else:
            out.loc[members] = (values.loc[members] - mu) / sigma

    # Instruments with no sector label still get scored, against the universe.
    unlabelled = aligned[aligned.isna()].index
    if len(unlabelled) and np.isfinite(u_sigma) and u_sigma != 0:
        out.loc[unlabelled] = (values.loc[unlabelled] - u_mu) / u_sigma

    return out
