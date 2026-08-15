"""Composite scoring — DESIGN §6.1 weights, §6.2 overlay, §6.3 signal mapping.

The pipeline at one date:

    raw factor  --sector z-score-->  z  --sign-->  family mean  --standardise-->
    weighted sum  --standardise-->  Phi()  -->  0-100  --overlay-->  signal

Four decisions in that chain are not obvious, and each of them changes the
answer.

**1. Signs are normalised before aggregation, not at selection.** Debt/equity and
accruals are lower-is-better. A weighted sum of z-scores that has not flipped
them subtracts quality from quality. The single-factor backtest could defer this
to `nlargest`/`nsmallest`; a composite cannot.

**2. Family scores are re-standardised before weighting.** Averaging six
correlated z-scores does not give something with unit variance — it gives
something with a variance set by how correlated they happen to be. Quality, with
six intercorrelated accounting ratios, would come out with a larger spread than
sentiment's single factor, so a nominal 30/10 split would not be the split that
was applied. Re-standardising each family makes the declared weights the actual
weights.

**3. The composite is re-standardised again before Phi.** A 0.25/0.30/0.20/0.15/
0.10 weighted sum of unit-variance, imperfectly-correlated series has a standard
deviation around 0.5, so `Phi` of it would compress the whole universe into
roughly 30-70 and DESIGN's 75 threshold would never be reached by anyone. The
consequence is that **the score is explicitly relative**: 75 means "top quartile
of this universe on this date", never "cheap in absolute terms". A universe of
uniformly overvalued companies still produces BUYs. That is a property of every
cross-sectional factor model and is stated here rather than discovered later.

**4. Missing data reduces coverage; it never scores as neutral.** See
`min_coverage`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.factors import growth, momentum, quality, sentiment, value
from stockanalysis.factors.base import Factor, sector_zscore
from stockanalysis.factors.panel import PANEL_CACHE, PanelCache
from stockanalysis.factors.redflags import evaluate as evaluate_flags
from stockanalysis.factors.redflags import summarise as summarise_flags

MODEL_NAME = "phase2-composite"

# DESIGN §6.1.
FAMILY_WEIGHTS: dict[str, float] = {
    "value": 0.25,
    "quality": 0.30,
    "growth": 0.20,
    "momentum": 0.15,
    "sentiment": 0.10,
}

# DESIGN §6.3.
BUY_THRESHOLD = 75.0
SELL_THRESHOLD = 45.0

BUY, HOLD, SELL = "BUY", "HOLD", "SELL"


def default_factors() -> list[Factor]:
    """Every factor in the model, in family order."""
    return [*value.ALL, *quality.ALL, *growth.ALL, *momentum.ALL, *sentiment.ALL]


def intra_family_weights(factors: list[Factor]) -> dict[str, float]:
    """Weight of each factor within its family.

    Equal within a family unless the family declares otherwise. Only quality
    does, because DESIGN singles out CFO/PAT and accruals as carrying
    disproportionate weight.
    """
    weights: dict[str, float] = {}
    for fam in {f.family for f in factors}:
        members = [f for f in factors if f.family == fam]
        declared = quality.INTRA_FAMILY_WEIGHTS if fam == "quality" else {}
        for f in members:
            weights[f.name] = declared.get(f.name, 1.0 / len(members))
    return weights


@dataclass
class ScoringConfig:
    family_weights: dict[str, float] = field(
        default_factory=lambda: dict(FAMILY_WEIGHTS)
    )
    buy_threshold: float = BUY_THRESHOLD
    sell_threshold: float = SELL_THRESHOLD

    # Fraction of the model's total weight that must be backed by real data
    # before a company is scored at all.
    #
    # This is the guard that stops the composite degenerating into whichever
    # single factor happens to have data. With fundamentals absent, a company
    # would otherwise be scored on momentum alone and ranked against companies
    # scored on all five families as though the two numbers meant the same
    # thing. 0.5 is a judgement, not a result; the honest way to run on partial
    # data is to lower it deliberately and have the run say so.
    min_coverage: float = 0.5

    apply_red_flags: bool = True
    min_sector_size: int = 5
    winsor_limit: float = 3.0

    def version(self) -> str:
        """Identifier recorded on every stored signal.

        Weights are part of the model. A backtest run under 30% quality and one
        under 40% are different models, and a `signals` table that cannot tell
        them apart is an audit trail that does not audit anything.
        """
        payload = json.dumps(
            {
                "weights": self.family_weights,
                "buy": self.buy_threshold,
                "sell": self.sell_threshold,
                "min_coverage": self.min_coverage,
                "red_flags": self.apply_red_flags,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:8]
        return f"{MODEL_NAME}-{digest}"


@dataclass
class ScoreResult:
    as_of: dt.date
    raw: pd.DataFrame          # isin x factor, before any transformation
    factor_z: pd.DataFrame     # isin x factor, sector-relative and sign-adjusted
    family_z: pd.DataFrame     # isin x family, standardised
    coverage: pd.Series        # fraction of model weight actually measured
    composite_z: pd.Series
    # What the factors said, before the overlay. Reported, never ranked on.
    factor_score: pd.Series    # 0-100, NaN where coverage was insufficient
    # What the backtest ranks on: `factor_score` with flagged names withdrawn.
    score: pd.Series
    flags: pd.DataFrame        # isin x flag, tri-state
    flag_summary: pd.DataFrame # tripped / unknown / has_red_flag
    signal: pd.Series
    version: str

    def table(self) -> pd.DataFrame:
        """Flat per-ISIN view for display and persistence.

        Reports `factor_score`, not the ranked score. A company the overlay
        removed still has a factor score, and hiding it behind a NaN loses the
        most useful thing about the overlay: that it fired *against* a company
        the factors liked. "82, SELL, auditor_qualification" is the finding.
        "NaN, SELL" is the same fact with the evidence deleted.
        """
        return pd.DataFrame(
            {
                "score": self.factor_score,
                "signal": self.signal,
                "coverage": self.coverage,
                "red_flags": self.flag_summary["tripped"].apply(",".join),
                "unknown_flags": self.flag_summary["unknown"].apply(",".join),
            }
        )


class CompositeModel(Factor):
    """The full factor model, presented as a Factor so the engine is unchanged.

    `compute` returns the 0-100 score with red-flagged names removed, which is
    what the backtest ranks on. `score` returns everything behind it — the
    factor-level z-scores, the family breakdown, coverage and the flag states —
    which is what the reporting commands and the attribution report read.
    """

    name = "composite"
    family = "composite"
    needs_sector_zscore = False   # already sector-relative; see base.Factor

    def __init__(
        self,
        factors: list[Factor] | None = None,
        config: ScoringConfig | None = None,
        cache: PanelCache | None = None,
    ) -> None:
        self.factors = factors if factors is not None else default_factors()
        self.config = config or ScoringConfig()
        self.cache = cache if cache is not None else PANEL_CACHE

    def compute(self, db: Database, isins: list[str], as_of: dt.date) -> pd.Series:
        if not isins:
            return pd.Series(dtype=float)
        return self.score(db, isins, as_of).score

    def score(self, db: Database, isins: list[str], as_of: dt.date) -> ScoreResult:
        isins = list(isins)
        cfg = self.config
        sectors = _sectors(db, isins)

        raw = pd.DataFrame(index=isins, dtype=float)
        zs = pd.DataFrame(index=isins, dtype=float)
        for f in self.factors:
            values = f.compute(db, isins, as_of).reindex(isins).astype(float)
            raw[f.name] = values
            z = sector_zscore(
                values, sectors,
                min_sector_size=cfg.min_sector_size,
                winsor_limit=cfg.winsor_limit,
            )
            # Decision 1: normalise direction here, once, for everything.
            zs[f.name] = z if f.higher_is_better else -z

        family_z, coverage = self._aggregate_families(zs)
        composite_z = self._combine(family_z)

        insufficient = coverage < cfg.min_coverage
        composite_z = composite_z.mask(insufficient)

        factor_score = _standardise(composite_z).apply(_normal_cdf) * 100.0

        panel = self.cache.get(db, isins, as_of)
        flags = evaluate_flags(panel)
        flag_summary = summarise_flags(flags)

        signal = self._signals(factor_score, flag_summary)

        score = factor_score
        if cfg.apply_red_flags:
            # DESIGN §6.2: the overlay caps rather than adjusts. For ranking
            # purposes a capped name must not be selectable at all, so the score
            # is withdrawn rather than reduced — a reduced score still wins if
            # everything else is worse. The pre-overlay number survives on
            # `factor_score` for reporting.
            score = factor_score.mask(
                flag_summary["has_red_flag"].reindex(isins).fillna(False)
            )

        return ScoreResult(
            as_of=as_of,
            raw=raw,
            factor_z=zs,
            family_z=family_z,
            coverage=coverage,
            composite_z=composite_z,
            factor_score=factor_score,
            score=score,
            flags=flags,
            flag_summary=flag_summary,
            signal=signal,
            version=cfg.version(),
        )

    # ------------------------------------------------------------------

    def _aggregate_families(
        self, zs: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.Series]:
        """Family scores and the fraction of total model weight behind each ISIN."""
        intra = intra_family_weights(self.factors)
        families = sorted({f.family for f in self.factors})

        family_z = pd.DataFrame(index=zs.index, dtype=float)
        coverage = pd.Series(0.0, index=zs.index, dtype=float)

        for fam in families:
            members = [f.name for f in self.factors if f.family == fam]
            if not members:
                continue
            block = zs[members]
            w = pd.Series({m: intra[m] for m in members}, dtype=float)

            present = block.notna()
            available_weight = present.mul(w, axis=1).sum(axis=1)
            total_weight = float(w.sum())

            weighted = block.mul(w, axis=1).sum(axis=1, min_count=1)
            mean_z = weighted / available_weight.where(available_weight > 0)

            # Decision 2: equalise family variance so the declared weights bind.
            family_z[fam] = _standardise(mean_z)

            intra_coverage = available_weight / total_weight
            coverage += self.config.family_weights.get(fam, 0.0) * intra_coverage

        return family_z, coverage

    def _combine(self, family_z: pd.DataFrame) -> pd.Series:
        """Weighted sum across families, renormalised over those present.

        Renormalisation is what makes a missing family a loss of *information*
        rather than a drag towards zero. A company with no news coverage should
        be ranked on the other 90% of the model, not penalised as though its
        sentiment were bad. The compensating guard is `min_coverage`: renormalise
        too far and the score stops meaning what it says, so below the threshold
        it is withdrawn instead.
        """
        weights = pd.Series(self.config.family_weights, dtype=float)
        weights = weights.reindex(family_z.columns).fillna(0.0)

        present = family_z.notna()
        denominator = present.mul(weights, axis=1).sum(axis=1)
        numerator = family_z.mul(weights, axis=1).sum(axis=1, min_count=1)
        return numerator / denominator.where(denominator > 0)

    def _signals(self, score: pd.Series, flag_summary: pd.DataFrame) -> pd.Series:
        cfg = self.config
        tripped = flag_summary["has_red_flag"].reindex(score.index).fillna(False)

        out = pd.Series(index=score.index, dtype=object)
        out[score >= cfg.buy_threshold] = BUY
        out[(score >= cfg.sell_threshold) & (score < cfg.buy_threshold)] = HOLD
        out[score < cfg.sell_threshold] = SELL
        if cfg.apply_red_flags:
            out[tripped.astype(bool)] = SELL
        # An unscored company is not a HOLD. It has no signal, and saying so is
        # the difference between "we looked and it was average" and "we could
        # not look".
        out[score.isna()] = None
        return out


# ----------------------------------------------------------------------


def _sectors(db: Database, isins: list[str]) -> pd.Series:
    df = db.query("SELECT isin, sector FROM instruments")
    if df.empty:
        return pd.Series(index=isins, dtype=object)
    return df.set_index("isin")["sector"].reindex(isins)


def _standardise(s: pd.Series) -> pd.Series:
    """Cross-sectional z-score. Zero-variance input maps to zero, not to NaN."""
    clean = s.dropna()
    if clean.empty:
        return s
    mu, sigma = clean.mean(), clean.std()
    if not np.isfinite(sigma) or sigma == 0:
        return s.where(s.isna(), 0.0)
    return (s - mu) / sigma


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via erf — avoids a scipy dependency for one function."""
    if not np.isfinite(z):
        return float("nan")
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def score_as_of(
    db: Database,
    index_name: str,
    as_of: dt.date,
    config: ScoringConfig | None = None,
) -> ScoreResult:
    """Score the index universe as it stood on `as_of`."""
    universe = db.as_of_universe(index_name, as_of)
    if not universe:
        raise ValueError(
            f"{index_name} had no members on {as_of}. Run `seed-universe` first."
        )
    return CompositeModel(config=config).score(db, universe, as_of)


def family_percentiles(result: ScoreResult) -> pd.DataFrame:
    """Family z-scores restated as 0-100 percentiles of the scored universe.

    The same transformation the composite itself goes through, applied per
    family, so a family score and the headline score are read off one scale. A
    family the company has no data for stays NaN — mapping it to 50 would assert
    "average" about something that was never measured.
    """
    out = pd.DataFrame(index=result.family_z.index, dtype=float)
    for family in result.family_z.columns:
        out[family] = _standardise(result.family_z[family]).apply(_normal_cdf) * 100.0
    return out


def persist(
    db: Database, result: ScoreResult, generate_narratives: bool = False
) -> tuple[int, int]:
    """Write factor-level scores and signals. Returns (factor rows, signal rows).

    With `generate_narratives`, each stored signal also gets a written
    explanation from Claude. Off by default: it is the only part of scoring that
    costs money per run, and every test would otherwise pay for it.
    """
    rows = []
    for factor_name in result.raw.columns:
        block = pd.DataFrame(
            {
                "isin": result.raw.index,
                "as_of_date": result.as_of,
                "factor_name": factor_name,
                "raw_value": result.raw[factor_name].to_numpy(),
                "sector_zscore": result.factor_z[factor_name].to_numpy(),
            }
        )
        rows.append(block[block["raw_value"].notna()])

    n_factors = 0
    if rows:
        n_factors = db.upsert_df(
            "factor_scores",
            pd.concat(rows, ignore_index=True),
            ["isin", "as_of_date", "factor_name"],
        )

    table = result.table()
    signals = pd.DataFrame(
        {
            "isin": table.index,
            "as_of_date": result.as_of,
            "composite_score": table["score"].to_numpy(),
            "signal": table["signal"].to_numpy(),
            "red_flags": table["red_flags"].to_numpy(),
            "unknown_flags": table["unknown_flags"].to_numpy(),
            "coverage": table["coverage"].to_numpy(),
            "narrative": None,
            "model_version": result.version,
        }
    )
    signals = signals[signals["signal"].notna()]

    if generate_narratives and not signals.empty:
        signals["narrative"] = _narratives(db, result, signals)

    n_signals = db.upsert_df("signals", signals, ["isin", "as_of_date"])

    return n_factors, n_signals


def _narratives(
    db: Database, result: ScoreResult, signals: pd.DataFrame
) -> list[str | None]:
    """Written explanations for each stored signal, aligned to `signals`' order.

    A company with no score has nothing to explain and is skipped rather than
    sent to the model with a NaN in the prompt.
    """
    from stockanalysis.serve.narrative import NarrativeGenerator, build_inputs

    percentiles = family_percentiles(result)
    meta = db.query(
        "SELECT isin, nse_symbol, name, sector FROM instruments"
    ).set_index("isin")

    rows = []
    for _, row in signals.iterrows():
        isin = row["isin"]
        if pd.isna(row["composite_score"]):
            continue
        scores = {}
        if isin in percentiles.index:
            scores = {
                family: float(value)
                for family, value in percentiles.loc[isin].items()
                if pd.notna(value)
            }
        info = meta.loc[isin] if isin in meta.index else None
        rows.append(
            {
                "isin": isin,
                "nse_symbol": info["nse_symbol"] if info is not None else isin,
                "name": info["name"] if info is not None else isin,
                "sector": info["sector"] if info is not None else None,
                "composite_score": float(row["composite_score"]),
                "signal": str(row["signal"]),
                "coverage": (
                    None if pd.isna(row["coverage"]) else float(row["coverage"])
                ),
                "red_flags": _split_flags(row["red_flags"]),
                "unknown_flags": _split_flags(row["unknown_flags"]),
                "family_scores": scores,
            }
        )

    if not rows:
        return [None] * len(signals)

    written = NarrativeGenerator().generate_many(
        build_inputs(db, result.as_of, rows)
    )
    return [written.get(isin) for isin in signals["isin"]]


def _split_flags(value: object) -> tuple[str, ...]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ()
    return tuple(part.strip() for part in str(value).split(",") if part.strip())
