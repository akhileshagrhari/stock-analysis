"""Why a signal is what it is — DESIGN §6.1/§6.2 read backwards.

The dashboard could already show *what* the model concluded. This turns the
stored numbers back into the reasoning behind them: which families carried the
score, which individual factors were the strongest arguments for and against,
and what the news mix and flag states contribute.

**It is reconstructed from `factor_scores`, not re-scored.** The stored
sector z-scores are the model's own intermediate values, so replaying the
aggregation over them reproduces the composite exactly — verified against a
fresh scoring run to floating-point equality. That means no API keys, no
recomputation of factors, and no risk of the explanation describing a different
number than the one on screen.

Two properties of the stored data make this honest rather than decorative:

**Stored z-scores are already sign-adjusted.** `persist` writes `factor_z`,
which the model flips for lower-is-better factors, so a positive z always means
"good for this company" — debt/equity included. Presenting it without knowing
that would invert the explanation for exactly the factors where being wrong
matters most.

**A z-score is relative to the company's own sector.** "Strong on ROE" here
means strong against its sector peers on the scoring date, never strong in the
abstract. Every label produced by this module is phrased that way.

The one thing reconstruction cannot recover is the config a stored signal was
produced under. Family weights are part of the model, so explaining an old
signal with today's weights would attribute it wrongly — `Explanation.stale`
flags that, and the caller is expected to say so rather than quietly proceed.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.factors import redflags
from stockanalysis.factors.composite import (
    BUY_THRESHOLD,
    SELL_THRESHOLD,
    CompositeModel,
    ScoringConfig,
    _normal_cdf,
    _standardise,
)
from stockanalysis.serve import queries
from stockanalysis.serve.queries import SentimentCounts, Signal

# A z-score is a distance from the sector mean in standard deviations. These are
# the bands the prose uses; they are descriptive, not thresholds the model acts
# on — the model itself only ever thresholds the final 0-100 score.
STRONG_Z = 0.75
WEAK_Z = -0.75

FAMILY_ORDER = ["value", "quality", "growth", "momentum", "sentiment"]

#: Plain-language names. The key is the stored `factor_name`; the value is what
#: an analyst would call it, plus what a *high* value means after the model's
#: sign adjustment — which is not always what the raw metric's name suggests.
FACTOR_LABELS: dict[str, tuple[str, str]] = {
    "earnings_yield": ("Earnings yield", "cheap on earnings"),
    "book_to_price": ("Book-to-price", "cheap on book value"),
    "ebitda_to_ev": ("EBITDA/EV", "cheap on enterprise value"),
    "fcf_yield": ("Free cash flow yield", "cheap on free cash flow"),
    "peg_inverse": ("Inverse PEG", "cheap relative to growth"),
    "roe": ("Return on equity", "earns well on shareholder funds"),
    "roce": ("Return on capital employed", "earns well on capital employed"),
    "debt_to_equity": ("Debt/equity", "carries less debt"),
    "interest_coverage": ("Interest coverage", "covers interest comfortably"),
    "cfo_to_pat": ("Cash conversion (CFO/PAT)", "profits arrive as cash"),
    "accruals": ("Accruals", "earnings backed by cash, not accruals"),
    "revenue_cagr_3y": ("Revenue CAGR (3y)", "revenue compounding faster"),
    "pat_cagr_3y": ("Profit CAGR (3y)", "profit compounding faster"),
    "quarterly_revenue_yoy": ("Revenue growth (YoY)", "revenue growing faster"),
    "quarterly_pat_yoy": ("Profit growth (YoY)", "profit growing faster"),
    "margin_trend": ("Margin trend", "margins improving"),
    "momentum_12_1": ("12-1 month momentum", "stronger price trend"),
    "price_to_200dma": ("Price vs 200-DMA", "trading above its trend"),
    "relative_strength_6m": ("Relative strength (6m)", "outperforming the index"),
    "news_sentiment_30d": ("News sentiment (30d)", "more positive news"),
}

# How each stored `raw_value` is to be read. Taken from the factor definitions
# in `factors/`, not from the names: `relative_strength_6m` is a return minus the
# universe's median return, so a difference in percentage points, and
# `peg_inverse` is earnings yield times profit growth — a product of two
# fractions with no unit of its own.
PERCENT = "percent"                # a fraction, shown as %: 0.0785 is 7.85%
PERCENTAGE_POINTS = "pp"           # a difference of two fractions: 0.012 is +1.2 pp
MULTIPLE = "multiple"              # a ratio, shown as ×: 0.68 is 0.68×
RATIO_VS_TREND = "ratio_vs_trend"  # price / its 200-day average: 0.89 is 11% below
SCORE = "score"                    # signed, −1 (bearish) to +1 (bullish)
RATIO = "ratio"                    # a plain number with no unit
FACTOR_UNITS: dict[str, str] = {
    "earnings_yield": PERCENT,
    "book_to_price": MULTIPLE,
    "ebitda_to_ev": PERCENT,
    "fcf_yield": PERCENT,
    "peg_inverse": RATIO,
    "roe": PERCENT,
    "roce": PERCENT,
    "debt_to_equity": MULTIPLE,
    "interest_coverage": MULTIPLE,
    "cfo_to_pat": MULTIPLE,
    "accruals": PERCENT,
    "revenue_cagr_3y": PERCENT,
    "pat_cagr_3y": PERCENT,
    "quarterly_revenue_yoy": PERCENT,
    "quarterly_pat_yoy": PERCENT,
    "margin_trend": PERCENTAGE_POINTS,
    "momentum_12_1": PERCENT,
    "price_to_200dma": RATIO_VS_TREND,
    "relative_strength_6m": PERCENTAGE_POINTS,
    "news_sentiment_30d": SCORE,
}
UNITS = (PERCENT, PERCENTAGE_POINTS, MULTIPLE, RATIO_VS_TREND, SCORE, RATIO)

FAMILY_MEANING = {
    "value": "how cheap it is against its sector",
    "quality": "balance-sheet strength and cash conversion",
    "growth": "how fast revenue and profit are compounding",
    "momentum": "price trend and relative strength",
    "sentiment": "tone of the last 30 days of news",
}


def ordinal(value: float) -> str:
    """1st, 2nd, 3rd, 4th — including the 11th-13th exceptions."""
    n = int(round(value))
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def factor_label(name: str) -> str:
    return FACTOR_LABELS.get(name, (name.replace("_", " ").capitalize(), ""))[0]


def factor_meaning(name: str) -> str:
    return FACTOR_LABELS.get(name, ("", ""))[1]


def factor_unit(name: str) -> str | None:
    """None for a name the current model does not define (an old stored row)."""
    return FACTOR_UNITS.get(name)


@dataclass(frozen=True)
class FamilyContribution:
    """One family's part in the composite."""

    family: str
    weight: float
    percentile: float | None   # 0-100 within the scored universe
    z: float | None
    # Share of the composite z this family supplied. Signed: negative means the
    # family pulled the score down.
    contribution: float | None
    factors_measured: int
    factors_total: int

    @property
    def measured(self) -> bool:
        return self.z is not None

    @property
    def verdict(self) -> str:
        if not self.measured:
            return "not measured"
        if self.z >= STRONG_Z:
            return "strength"
        if self.z <= WEAK_Z:
            return "weakness"
        return "neutral"


@dataclass(frozen=True)
class FactorDriver:
    """One factor's argument for or against, in the company's own numbers."""

    name: str
    family: str
    raw_value: float | None
    z: float | None

    @property
    def label(self) -> str:
        return factor_label(self.name)

    @property
    def meaning(self) -> str:
        return factor_meaning(self.name)

    @property
    def unit(self) -> str | None:
        return factor_unit(self.name)


@dataclass(frozen=True)
class Explanation:
    signal: Signal
    headline: str
    reasons: list[str] = field(default_factory=list)
    families: list[FamilyContribution] = field(default_factory=list)
    strengths: list[FactorDriver] = field(default_factory=list)
    weaknesses: list[FactorDriver] = field(default_factory=list)
    news: SentimentCounts | None = None
    # True when the stored signal came from a different scoring config than the
    # one now loaded, so the weights used here are not the weights that produced
    # the score.
    stale: bool = False
    stored_version: str | None = None
    current_version: str | None = None


# ----------------------------------------------------------------------


def factor_panel(db: Database, as_of: dt.date) -> pd.DataFrame:
    """isin x factor matrix of stored, sign-adjusted sector z-scores.

    The universe-wide panel, not one row: the family percentile a company gets
    is its rank *within the scored universe*, so the whole cross-section has to
    be present to compute it. Fetched once per explanation and passed down.
    """
    rows = db.query(
        "SELECT isin, factor_name, sector_zscore FROM factor_scores "
        "WHERE as_of_date = ?",
        [as_of],
    )
    if rows.empty:
        return pd.DataFrame()
    return rows.pivot(index="isin", columns="factor_name", values="sector_zscore")


def raw_values(db: Database, isin: str, as_of: dt.date) -> dict[str, float | None]:
    """The company's own factor values, in their natural units."""
    rows = db.query(
        "SELECT factor_name, raw_value FROM factor_scores "
        "WHERE isin = ? AND as_of_date = ?",
        [isin, as_of],
    )
    return {
        str(row["factor_name"]): queries._opt_float(row["raw_value"])
        for _, row in rows.iterrows()
    }


def _family_rows(
    panel: pd.DataFrame,
    isin: str,
    model: CompositeModel,
) -> list[FamilyContribution]:
    """Replay the model's family aggregation over the stored z-scores."""
    if panel.empty or isin not in panel.index:
        return []

    family_z, _coverage = model._aggregate_families(panel)
    weights = (
        pd.Series(model.config.family_weights, dtype=float)
        .reindex(family_z.columns)
        .fillna(0.0)
    )

    # Contribution decomposes the composite exactly: the weighted family z,
    # renormalised over the families actually present, is how `_combine` builds
    # the composite in the first place. Summing these gives the composite z back.
    present = family_z.notna()
    denominator = present.mul(weights, axis=1).sum(axis=1)
    contributions = family_z.mul(weights, axis=1).div(
        denominator.where(denominator > 0), axis=0
    )

    rows: list[FamilyContribution] = []
    for family in family_z.columns:
        members = [f.name for f in model.factors if f.family == family]
        percentiles = _standardise(family_z[family]).apply(_normal_cdf) * 100.0
        rows.append(
            FamilyContribution(
                family=str(family),
                weight=float(weights.get(family, 0.0)),
                percentile=queries._opt_float(percentiles.loc[isin]),
                z=queries._opt_float(family_z.loc[isin, family]),
                contribution=queries._opt_float(contributions.loc[isin, family]),
                factors_measured=int(panel.loc[isin, members].notna().sum()),
                factors_total=len(members),
            )
        )

    order = {name: i for i, name in enumerate(FAMILY_ORDER)}
    rows.sort(key=lambda r: order.get(r.family, len(order)))
    return rows


def _drivers(
    panel: pd.DataFrame,
    isin: str,
    raws: dict[str, float | None],
    model: CompositeModel,
) -> tuple[list[FactorDriver], list[FactorDriver]]:
    """Individual factors sorted into the case for and the case against.

    Sorting on the stored z is only meaningful because it is sign-adjusted:
    the most negative z is the worst argument, whichever direction the
    underlying metric runs.
    """
    if panel.empty or isin not in panel.index:
        return [], []

    families = {f.name: f.family for f in model.factors}
    drivers = [
        FactorDriver(
            name=str(name),
            family=families.get(str(name), "other"),
            raw_value=raws.get(str(name)),
            z=queries._opt_float(value),
        )
        for name, value in panel.loc[isin].items()
    ]
    measured = [d for d in drivers if d.z is not None]

    strengths = sorted(
        (d for d in measured if d.z > 0), key=lambda d: d.z, reverse=True
    )
    weaknesses = sorted((d for d in measured if d.z < 0), key=lambda d: d.z)
    return strengths, weaknesses


def _headline(signal: Signal) -> str:
    if signal.red_flags:
        flags = ", ".join(signal.red_flags)
        if signal.composite_score is None:
            return f"SELL — red flag ({flags}); the factors could not be scored."
        return (
            f"SELL — forced by the red-flag overlay ({flags}), overriding a "
            f"factor score of {signal.composite_score:.0f}/100."
        )
    if signal.composite_score is None or signal.signal is None:
        return (
            "No signal — coverage fell below the model's floor, so the score "
            "was withdrawn rather than guessed."
        )
    score = signal.composite_score
    if signal.signal == "BUY":
        return f"BUY — {score:.0f}/100, at or above the {BUY_THRESHOLD:.0f} threshold."
    if signal.signal == "SELL":
        return f"SELL — {score:.0f}/100, below the {SELL_THRESHOLD:.0f} threshold."
    return (
        f"HOLD — {score:.0f}/100, between the {SELL_THRESHOLD:.0f} and "
        f"{BUY_THRESHOLD:.0f} thresholds."
    )


def _reasons(
    signal: Signal,
    families: list[FamilyContribution],
    strengths: list[FactorDriver],
    weaknesses: list[FactorDriver],
    news: SentimentCounts | None,
) -> list[str]:
    """The case in sentences, strongest argument first."""
    out: list[str] = []

    if signal.red_flags:
        for flag in signal.red_flags:
            rule = next(
                (d.description for d in redflags.DEFINITIONS if d.name == flag), flag
            )
            out.append(f"Red flag — {rule}. This caps the signal at SELL on its own.")

    ranked = [f for f in families if f.contribution is not None]
    helped = sorted(ranked, key=lambda f: f.contribution, reverse=True)
    for family in helped[:2]:
        if family.contribution > 0 and family.z is not None:
            out.append(
                f"{family.family.capitalize()} is pulling the score up — "
                f"{FAMILY_MEANING.get(family.family, '')}, ranking around the "
                f"{ordinal(family.percentile)} percentile of the universe."
            )
    for family in sorted(ranked, key=lambda f: f.contribution)[:2]:
        if family.contribution < 0 and family.z is not None:
            out.append(
                f"{family.family.capitalize()} is pulling it down — "
                f"{FAMILY_MEANING.get(family.family, '')}, around the "
                f"{ordinal(family.percentile)} percentile."
            )

    for driver in strengths[:2]:
        if driver.z is not None and driver.z >= STRONG_Z and driver.meaning:
            out.append(
                f"Strongest single factor is {driver.label.lower()} "
                f"({driver.z:+.1f} SD vs sector): {driver.meaning}."
            )
            break
    for driver in weaknesses[:2]:
        if driver.z is not None and driver.z <= WEAK_Z and driver.meaning:
            out.append(
                f"Weakest single factor is {driver.label.lower()} "
                f"({driver.z:+.1f} SD vs sector)."
            )
            break

    if news is not None and news.total:
        if news.positive > news.negative:
            tone = "net positive"
        elif news.negative > news.positive:
            tone = "net negative"
        else:
            tone = "mixed"
        out.append(
            f"News over 30 days is {tone}: {news.positive} positive, "
            f"{news.negative} negative, {news.neutral} neutral."
        )
    else:
        out.append(
            "No scored news in the last 30 days, so sentiment contributed "
            "nothing and the score rests on the other families."
        )

    unmeasured = [f.family for f in families if not f.measured]
    if unmeasured:
        out.append(
            "Not measured for want of data: "
            + ", ".join(unmeasured)
            + ". The remaining families were reweighted to fill the gap."
        )

    if signal.unknown_flags:
        out.append(
            "These red flags could not be evaluated: "
            + ", ".join(signal.unknown_flags)
            + ". Their absence is not a clean bill of health."
        )

    return out


def dominant_families(
    db: Database,
    as_of: dt.date,
    config: ScoringConfig | None = None,
) -> dict[str, tuple[str, float]]:
    """Per ISIN, the family that moved its score most — `isin -> (family, contribution)`.

    One panel computation for the whole universe, so a signal table can show why
    each row scored as it did without a query per row.
    """
    model = CompositeModel(config=config or ScoringConfig())
    panel = factor_panel(db, as_of)
    if panel.empty:
        return {}

    panel = panel.reindex(columns=[f.name for f in model.factors])
    family_z, _coverage = model._aggregate_families(panel)
    weights = (
        pd.Series(model.config.family_weights, dtype=float)
        .reindex(family_z.columns)
        .fillna(0.0)
    )
    present = family_z.notna()
    denominator = present.mul(weights, axis=1).sum(axis=1)
    contributions = family_z.mul(weights, axis=1).div(
        denominator.where(denominator > 0), axis=0
    )

    out: dict[str, tuple[str, float]] = {}
    for isin, row in contributions.iterrows():
        clean = row.dropna()
        if clean.empty:
            continue
        family = clean.abs().idxmax()
        out[str(isin)] = (str(family), float(clean[family]))
    return out


def explain(
    db: Database,
    isin: str,
    as_of: dt.date | None = None,
    config: ScoringConfig | None = None,
) -> Explanation | None:
    """Full reasoning behind one stored signal. None if there is no signal.

    The latest signal when `as_of` is None, else the one stored for exactly that
    date — never the nearest. Everything below reads the same date the signal
    was stored for: an explanation pairing today's score with another day's
    factors would describe a number nobody was shown.
    """
    signal = (
        queries.latest_signal(db, isin) if as_of is None
        else queries.signal_on(db, isin, as_of)
    )
    if signal is None:
        return None
    as_of = signal.as_of

    config = config or ScoringConfig()
    model = CompositeModel(config=config)
    panel = factor_panel(db, as_of)
    if not panel.empty:
        panel = panel.reindex(columns=[f.name for f in model.factors])

    families = _family_rows(panel, isin, model)
    strengths, weaknesses = _drivers(panel, isin, raw_values(db, isin, as_of), model)
    news = queries.sentiment_counts(db, [isin], as_of).get(isin)

    current_version = config.version()
    return Explanation(
        signal=signal,
        headline=_headline(signal),
        reasons=_reasons(signal, families, strengths, weaknesses, news),
        families=families,
        strengths=strengths,
        weaknesses=weaknesses,
        news=news,
        stale=(
            signal.model_version is not None
            and signal.model_version != current_version
        ),
        stored_version=signal.model_version,
        current_version=current_version,
    )


# ----------------------------------------------------------------------
# Why a company is, or is not, scored
# ----------------------------------------------------------------------

SCORED = "scored"
NOT_IN_LATEST_RUN = "not_in_latest_run"
BELOW_COVERAGE_FLOOR = "below_coverage_floor"
MISSING_FUNDAMENTALS = "missing_fundamentals"
SECTOR_NOT_SUPPORTED = "sector_not_supported"
NEVER_SCORED = "never_scored"
SCORE_REASONS = (
    SCORED,
    NOT_IN_LATEST_RUN,
    BELOW_COVERAGE_FLOOR,
    MISSING_FUNDAMENTALS,
    SECTOR_NOT_SUPPORTED,
    NEVER_SCORED,
)

# Results formats the annual reader cannot build statements from, by a word in
# the exchange's own file name (…/xbrl/INTEGRATED_FILING_BANKING_1740913_….xml).
# Every annual filing in these formats has been read and refused: banks tag no
# revenue line or total equity, life insurers no revenue, profit or equity.
_UNSUPPORTED_FORMATS = {"BANKING": "banks", "LI": "insurers", "GI": "insurers"}
_FORMAT = re.compile(r"/([A-Z_]+?)_\d")


@dataclass(frozen=True)
class ScoreStatus:
    """What the stored runs say about one company's score, read back.

    `measured_as_of` is the run the reason describes: the newest date a run
    stored anything for the company. It can be later than `latest_as_of` — a
    run that measured the company and could not score it stores factor rows but
    no signal, so the older signal is what `/latest` still returns.
    """

    isin: str
    scored: bool
    reason: str
    latest_as_of: dt.date | None
    measured_as_of: dt.date | None
    coverage: float | None
    coverage_floor: float
    factors_measured: int | None
    factors_total: int
    detail: str | None


def results_format(xbrl_url: str | None) -> str | None:
    """The exchange's name for a filing's results format, from its file name."""
    match = _FORMAT.search(xbrl_url or "")
    return match.group(1) if match else None


def _unsupported(xbrl_url: str | None) -> str | None:
    """'banks' or 'insurers' when the filing is in a format the model can't read."""
    words = (results_format(xbrl_url) or "").split("_")
    for word, kind in _UNSUPPORTED_FORMATS.items():
        if word in words:
            return kind
    return None


def _day(date: dt.date) -> str:
    return f"{date.day} {date:%b %Y}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def score_status(
    db: Database, isin: str, config: ScoringConfig | None = None
) -> ScoreStatus:
    """Whether a company has a score, and when it does not, the reason.

    Read from what runs stored, never re-scored: a run writes a factor row for
    every company it could measure at all, and a signal only for the ones that
    cleared the coverage floor. Replaying the model's family aggregation over a
    company's stored z-scores gives back the coverage that run saw — the number
    it compared with the floor.
    """
    config = config or ScoringConfig()
    model = CompositeModel(config=config)
    names = [f.name for f in model.factors]
    floor = config.min_coverage

    dates = db.query(
        "SELECT (SELECT MAX(as_of_date) FROM signals WHERE isin = ?) AS last_signal, "
        "(SELECT MAX(as_of_date) FROM factor_scores WHERE isin = ?) AS last_measured, "
        "(SELECT MAX(as_of_date) FROM signals) AS last_scored_run, "
        "(SELECT MAX(as_of_date) FROM factor_scores) AS last_measured_run",
        [isin, isin],
    ).iloc[0]
    last_signal = queries._opt_date(dates["last_signal"])
    last_measured = queries._opt_date(dates["last_measured"])
    runs = [
        d for d in (
            queries._opt_date(dates["last_scored_run"]),
            queries._opt_date(dates["last_measured_run"]),
        ) if d is not None
    ]
    measured_on = max((d for d in (last_signal, last_measured) if d), default=None)

    def status(
        scored: bool,
        reason: str,
        detail: str,
        coverage: float | None = None,
        factors_measured: int | None = None,
    ) -> ScoreStatus:
        return ScoreStatus(
            isin=isin,
            scored=scored,
            reason=reason,
            latest_as_of=last_signal,
            measured_as_of=measured_on,
            coverage=coverage,
            coverage_floor=floor,
            factors_measured=factors_measured,
            factors_total=len(names),
            detail=detail,
        )

    if measured_on is None:
        return status(False, NEVER_SCORED, _never_measured(db, isin))

    rows = db.query(
        "SELECT factor_name, sector_zscore FROM factor_scores "
        "WHERE isin = ? AND as_of_date = ?",
        [isin, measured_on],
    )
    n_measured = coverage = None
    unmeasured: list[str] = []
    if not rows.empty:
        panel = (
            rows.set_index("factor_name")["sector_zscore"]
            .astype(float).reindex(names).to_frame(isin).T
        )
        _family_z, replayed = model._aggregate_families(panel)
        coverage = queries._opt_float(replayed.iloc[0])
        n_measured = int(panel.iloc[0].notna().sum())
        unmeasured = [
            fam for fam in FAMILY_ORDER
            if not panel.iloc[0][[f.name for f in model.factors if f.family == fam]]
            .notna().any()
        ]
    counted = (
        f" ({n_measured} of {len(names)} factors)" if n_measured is not None else ""
    )

    if last_signal is not None and last_signal >= measured_on:
        signal = queries.signal_on(db, isin, last_signal)
        if signal is not None and signal.coverage is not None:
            coverage = signal.coverage
        share = f"{coverage:.0%} of the model's weight" if coverage is not None else None
        latest_run = max(runs, default=last_signal)
        if last_signal >= latest_run:
            detail = f"Scored on {_day(last_signal)}" + (
                f" with {share} measured{counted}." if share else "."
            )
            return status(
                True, SCORED, detail, coverage=coverage, factors_measured=n_measured
            )
        detail = (
            f"Last scored on {_day(last_signal)}; the latest run, on "
            f"{_day(latest_run)}, did not measure this company."
        )
        return status(
            True, NOT_IN_LATEST_RUN, detail,
            coverage=coverage, factors_measured=n_measured,
        )

    # Measured on `measured_on` and not scored.
    weight = f"{coverage or 0.0:.0%} of the model's weight"
    shortfall = (
        f"below the {floor:.0%} floor"
        if coverage is None or coverage < floor
        # Only a run made under a stricter floor than today's leaves this.
        else "yet that run stored no signal for it"
    )
    annual = db.query(
        "SELECT COUNT(*) AS n FROM fundamentals_annual WHERE isin = ? AND filing_date <= ?",
        [isin, measured_on],
    ).iloc[0]["n"]
    if int(annual) == 0:
        latest_filing = db.query(
            "SELECT xbrl_url FROM fundamentals_quarterly "
            "WHERE isin = ? AND xbrl_url IS NOT NULL "
            "ORDER BY period_end_date DESC LIMIT 1",
            [isin],
        )
        kind = _unsupported(
            None if latest_filing.empty else latest_filing.iloc[0]["xbrl_url"]
        )
        if kind is not None:
            detail = (
                f"The model can't read annual statements from {kind}' results "
                f"filings, so only {weight}{counted} could be measured on "
                f"{_day(measured_on)}, {shortfall}."
            )
            return status(
                False, SECTOR_NOT_SUPPORTED, detail,
                coverage=coverage, factors_measured=n_measured,
            )
        detail = (
            f"No annual results had been stored for this company by "
            f"{_day(measured_on)}, so only {weight}{counted} could be measured, "
            f"{shortfall}."
        )
        return status(
            False, MISSING_FUNDAMENTALS, detail,
            coverage=coverage, factors_measured=n_measured,
        )

    gaps = f"; nothing was measured for {_and(unmeasured)}" if unmeasured else ""
    detail = (
        f"Only {weight}{counted} could be measured on {_day(measured_on)}, "
        f"{shortfall}{gaps}."
    )
    return status(
        False, BELOW_COVERAGE_FLOOR, detail,
        coverage=coverage, factors_measured=n_measured,
    )


def _never_measured(db: Database, isin: str) -> str:
    held = db.query(
        "SELECT (SELECT COUNT(*) FROM prices_daily WHERE isin = ?) AS prices, "
        "(SELECT COUNT(*) FROM fundamentals_annual WHERE isin = ?) "
        "+ (SELECT COUNT(*) FROM fundamentals_quarterly WHERE isin = ?) AS results",
        [isin, isin, isin],
    ).iloc[0]
    missing = [name for name in ("prices", "results") if int(held[name]) == 0]
    if missing:
        return (
            f"No scoring run has measured this company; no {' or '.join(missing)} "
            f"are stored for it."
        )
    return "No scoring run has measured this company yet."
