"""Factor attribution — DESIGN §7's "factor attribution, decile spread".

A backtest tells you whether a *portfolio* made money. It cannot tell you which
factor was responsible, and with fifteen factors and one NAV curve there is no
way to find out after the fact. Attribution measures each factor separately,
against the same forward returns, on the same dates.

Two measures, because they fail differently:

**Rank information coefficient.** The Spearman correlation between a factor's
cross-sectional ranking on date t and realised returns over (t, t+1]. It uses
the whole cross-section, so it is the more statistically efficient of the two,
and being rank-based it is unbothered by the fat tails that make a Pearson
correlation on returns close to meaningless. Published equity factors live
around 0.02-0.05; anything above 0.15 sustained should be read as a bug report,
not a discovery.

**Decile spread.** Mean forward return of the top decile minus the bottom. Less
efficient — it discards 80% of the cross-section — but it answers the question
a portfolio actually poses, and it catches non-monotonic factors that a rank
correlation smooths over. A factor can post a respectable IC while its top
decile underperforms its second, which matters a great deal if you intend to
buy the top decile.

The number that decides whether an IC is real is not its mean, it is its
t-statistic: mean(IC) / std(IC) * sqrt(periods). A mean IC of 0.04 over 12
monthly observations is noise. The same figure over 120 is a factor.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.factors.base import Factor, sector_zscore

log = logging.getLogger(__name__)

# Below this the cross-sectional correlation on that date is not a measurement.
MIN_CROSS_SECTION = 10

# 10 for deciles. Falls back to fewer buckets on a small universe rather than
# comparing a "decile" of three names against another of three.
N_BUCKETS = 10
MIN_PER_BUCKET = 3


@dataclass
class FactorAttribution:
    factor: str
    family: str
    periods: int              # rebalances where the factor was computable
    coverage: float           # mean fraction of the universe with a value
    mean_ic: float
    ic_std: float
    ic_t_stat: float
    ic_hit_rate: float        # fraction of periods with IC in the expected direction
    decile_spread: float      # per-period, top minus bottom
    decile_spread_annualised: float
    monotonic: bool           # do bucket means increase across buckets?

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def run_attribution(
    db: Database,
    index_name: str,
    dates: list[dt.date],
    factors: list[Factor],
    periods_per_year: int = 12,
    min_universe_size: int = 10,
) -> pd.DataFrame:
    """Per-factor IC and decile statistics over the given rebalance dates.

    Signs are normalised, so a positive IC always means "the factor worked".
    Without that, debt/equity — where low is good — would report a negative IC
    and read as a failure while doing exactly what it is supposed to.
    """
    sectors = db.query("SELECT isin, sector FROM instruments").set_index("isin")["sector"]

    ics: dict[str, list[float]] = {f.name: [] for f in factors}
    spreads: dict[str, list[float]] = {f.name: [] for f in factors}
    buckets: dict[str, list[pd.Series]] = {f.name: [] for f in factors}
    coverage: dict[str, list[float]] = {f.name: [] for f in factors}

    for i in range(len(dates) - 1):
        t, t_next = dates[i], dates[i + 1]

        universe = db.as_of_universe(index_name, t)
        if len(universe) < min_universe_size:
            continue

        fwd = db.forward_returns(universe, t, t_next).dropna()
        if len(fwd) < MIN_CROSS_SECTION:
            continue

        for f in factors:
            raw = f.compute(db, universe, t).reindex(universe).astype(float)
            coverage[f.name].append(float(raw.notna().mean()))

            z = raw if not f.needs_sector_zscore else sector_zscore(raw, sectors)
            if not f.higher_is_better:
                z = -z

            aligned = pd.concat([z.rename("z"), fwd.rename("r")], axis=1).dropna()
            if len(aligned) < MIN_CROSS_SECTION:
                continue

            ic = _spearman(aligned["z"], aligned["r"])
            if pd.notna(ic):
                ics[f.name].append(float(ic))

            bucket_means = _bucket_means(aligned["z"], aligned["r"])
            if bucket_means is not None:
                buckets[f.name].append(bucket_means)
                spreads[f.name].append(
                    float(bucket_means.iloc[-1] - bucket_means.iloc[0])
                )

    rows = []
    for f in factors:
        rows.append(
            _summarise(f, ics[f.name], spreads[f.name], buckets[f.name],
                       coverage[f.name], periods_per_year).to_dict()
        )

    df = pd.DataFrame(rows)
    return df.sort_values("ic_t_stat", ascending=False, ignore_index=True)


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Rank correlation, computed as Pearson on ranks.

    That is the definition, and it avoids pulling in scipy — which pandas'
    `corr(method="spearman")` imports lazily, so the dependency would only
    surface at the moment the attribution report was run.
    """
    if len(a) < 2:
        return float("nan")
    return float(a.rank().corr(b.rank()))


def _bucket_means(z: pd.Series, fwd: pd.Series) -> pd.Series | None:
    """Mean forward return per factor bucket, lowest bucket first."""
    n_buckets = min(N_BUCKETS, len(z) // MIN_PER_BUCKET)
    if n_buckets < 2:
        return None
    try:
        # rank(method="first") breaks ties positionally. Without it a factor
        # with many identical values — a capped interest coverage, a universe
        # of zeroes — collapses into fewer bins than requested and qcut raises.
        labels = pd.qcut(z.rank(method="first"), n_buckets, labels=False)
    except ValueError:
        return None
    return fwd.groupby(labels).mean().sort_index()


def _summarise(
    factor: Factor,
    ics: list[float],
    spreads: list[float],
    buckets: list[pd.Series],
    coverage: list[float],
    periods_per_year: int,
) -> FactorAttribution:
    ic_series = pd.Series(ics, dtype=float)
    spread_series = pd.Series(spreads, dtype=float)

    mean_ic = float(ic_series.mean()) if len(ic_series) else float("nan")
    ic_std = float(ic_series.std()) if len(ic_series) > 1 else float("nan")
    t_stat = (
        float(mean_ic / ic_std * np.sqrt(len(ic_series)))
        if ic_std and np.isfinite(ic_std) and ic_std > 0
        else float("nan")
    )

    spread = float(spread_series.mean()) if len(spread_series) else float("nan")
    annualised = spread * periods_per_year if np.isfinite(spread) else float("nan")

    return FactorAttribution(
        factor=factor.name,
        family=factor.family,
        periods=len(ic_series),
        coverage=float(np.mean(coverage)) if coverage else 0.0,
        mean_ic=mean_ic,
        ic_std=ic_std,
        ic_t_stat=t_stat,
        ic_hit_rate=float((ic_series > 0).mean()) if len(ic_series) else float("nan"),
        decile_spread=spread,
        decile_spread_annualised=annualised,
        monotonic=_is_monotonic(buckets),
    )


def _is_monotonic(buckets: list[pd.Series]) -> bool:
    """Whether mean returns rise across buckets, averaged over all dates.

    Averaged first, then checked — a per-date monotonicity test on a noisy
    cross-section is almost never satisfied and would report False for every
    factor including the ones that work.
    """
    if not buckets:
        return False
    avg = pd.concat(buckets, axis=1).mean(axis=1).sort_index()
    return bool(avg.is_monotonic_increasing)


def format_attribution(df: pd.DataFrame, title: str = "Factor attribution") -> str:
    if df.empty:
        return f"{title}\n  (nothing computable)"

    # Kept inside 80 columns: the report is read in a terminal, and a table that
    # wraps every row is harder to scan than one with tighter columns.
    lines = [
        "",
        title,
        "=" * len(title),
        f"  {'factor':<22}{'family':<10}{'n':>4}{'cov':>6}{'IC':>7}"
        f"{'t':>6}{'hit':>6}{'spread/yr':>10}{'mono':>5}",
        f"  {'-' * 76}",
    ]
    for r in df.itertuples(index=False):
        lines.append(
            f"  {r.factor:<22}{r.family:<10}{r.periods:>4}"
            f"{r.coverage:>6.0%}{r.mean_ic:>7.3f}{r.ic_t_stat:>6.2f}"
            f"{r.ic_hit_rate:>6.0%}{r.decile_spread_annualised:>10.1%}"
            f"{'yes' if r.monotonic else 'no':>5}"
        )
    lines += [
        f"  {'-' * 76}",
        "  IC = mean rank correlation with next-period return, sign-normalised so",
        "  positive always means the factor worked. |t| < 2 is not evidence of",
        "  anything. Published equity factors sit around 0.02-0.05; a sustained",
        "  IC above 0.15 is more likely a leak than a discovery.",
        "",
    ]
    return "\n".join(lines)
