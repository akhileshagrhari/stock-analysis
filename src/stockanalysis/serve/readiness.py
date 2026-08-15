"""Per-company data readiness — what we hold, what is missing, and what fills it.

`explain.py` answers "why this score". This module answers the question that
comes *before* it: **is there enough data to have a score at all, and if not,
what exactly is absent and which pipeline step supplies it.**

That question has no home anywhere else in the system. `status` counts rows per
table across the whole universe, which cannot tell you that RELIANCE has prices
but no cash flow statement. The composite reports a single `coverage` number,
which says how much of the model was measured but not *which* part. And the
Instrument dashboard page returns early when there is no stored signal — so the
one screen you would visit to find out why a company is unscored goes blank in
exactly that case.

THREE STATES, NOT TWO
---------------------
A dataset is `PRESENT`, `PARTIAL` or `ABSENT`, and the middle one carries the
weight. Prices that stop eight months ago, two years of annual reports where a
3-year CAGR needs three, a downloaded PDF that was never extracted — all of
these are rows in a table. A boolean "have data?" reports them as a green tick
and then the factor silently comes back NaN. `PARTIAL` is what stops a coverage
gap from looking like a bug in the factor.

WHY THE FACTOR REQUIREMENTS ARE DECLARED HERE
---------------------------------------------
`NEEDS` restates, per factor, which datasets and which extracted fields it reads.
That is duplication of what the factor bodies already say, and it can drift —
`test_readiness.py` asserts every factor in `default_factors()` has an entry, and
that every declared field exists on `fundamentals_annual`, which catches the two
ways it drifts in practice.

The alternative was to infer the reason for a NaN from the factor itself, and
that is not possible: `safe_divide` returns NaN for "the input was missing" and
for "the denominator was negative" alike, and those are opposite findings. One
means *fetch more data*, the other means *this company is loss-making and the
ratio is meaningless for it*. Telling an operator to re-run an ingest that will
change nothing is worse than saying nothing.

WHAT THIS DOES NOT REPORT
-------------------------
The composite score. Scoring is cross-sectional — every factor is a
sector-relative z-score — so a one-company universe scores 50/100 by
construction, whatever the company. `coverage` and the red flags *are*
per-company and are computed live here; the score and signal are read from
whatever the last real universe run persisted, with its date shown, so the two
numbers on screen can never come from different models.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.factors import redflags, sentiment
from stockanalysis.factors.composite import (
    FAMILY_WEIGHTS,
    CompositeModel,
    ScoringConfig,
    default_factors,
    intra_family_weights,
)
from stockanalysis.factors.panel import PANEL_CACHE, Panel

# How stale prices may be before the momentum and value families are reading a
# quote nobody could have traded on. Matches the 45-day tolerance `panel`
# already applies when it looks up the last close.
PRICE_STALE_DAYS = 45

# How far behind the decision date the newest *period end* may sit before the
# quarterly and shareholding sources are reporting history rather than the
# present. Both are quarterly disclosures with a filing lag — 45 days under
# LODR for results, 21 for shareholding — so one missed quarter is normal and
# three is a stale feed. PHASE1-FINDINGS §3.2 records NSE's `results_comparison`
# returning "the last 5 quarters" that end a year or more ago; the knowledge
# date on those rows is honest, which is exactly why nothing downstream
# complains and the staleness has to be surfaced here.
STALE_PERIOD_DAYS = 270


class Have(StrEnum):
    PRESENT = "PRESENT"
    PARTIAL = "PARTIAL"
    ABSENT = "ABSENT"


# ----------------------------------------------------------------------
# Datasets — the unit an operator can actually act on
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetSpec:
    """One source of data, and the pipeline step that fetches it.

    `step` is a key in `run.steps.STEPS`, not a description of one. A readiness
    report that names a step the runner does not have is a dead end, and the
    registry is the only place that knows which steps exist.
    """

    key: str
    label: str
    step: str
    note: str = ""


DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        "prices", "Daily prices", "prices",
        "yfinance, split/bonus adjusted. Feeds momentum and every market-cap ratio.",
    ),
    DatasetSpec(
        "quarterly", "Quarterly results", "quarterly",
        "NSE results_comparison. Free, and the only fundamental source that "
        "needs no model in the loop.",
    ),
    DatasetSpec(
        "shareholding", "Shareholding pattern", "shareholding",
        "Promoter / FII / DII by quarter. Feeds two red flags, no factor.",
    ),
    DatasetSpec(
        "filings", "Annual report PDFs", "filings",
        "Downloaded from NSE. On its own it feeds nothing — it is what "
        "extraction reads.",
    ),
    DatasetSpec(
        "annual", "Extracted annual financials", "extract",
        "Claude reads the PDFs into the schema. The value, quality and growth "
        "families are built almost entirely from this.",
    ),
    DatasetSpec(
        "news", "Scored news", "sentiment",
        "Headlines resolved to this company and scored by FinBERT.",
    ),
)

DATASETS_BY_KEY: dict[str, DatasetSpec] = {d.key: d for d in DATASETS}


# ----------------------------------------------------------------------
# What each factor reads. See the module docstring on why this is declared.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Need:
    datasets: tuple[str, ...]
    annual_fields: tuple[str, ...] = ()
    quarterly_fields: tuple[str, ...] = ()
    # Distinct annual reports required. A CAGR over one observation is a level.
    min_annual_years: int = 1
    # Quarters required. A year-on-year comparison needs the same quarter a year
    # back, which is five rows once, not two.
    min_quarters: int = 0
    min_price_rows: int = 0
    min_articles: int = 0


NEEDS: dict[str, Need] = {
    # value
    "earnings_yield": Need(("annual", "prices"), annual_fields=("eps",)),
    "book_to_price": Need(
        ("annual", "prices"), annual_fields=("total_equity", "pat", "eps")
    ),
    "ebitda_to_ev": Need(
        ("annual", "prices"),
        annual_fields=("profit_before_tax", "depreciation", "pat", "eps"),
    ),
    "fcf_yield": Need(
        ("annual", "prices"), annual_fields=("ocf", "capex", "pat", "eps")
    ),
    "peg_inverse": Need(
        ("annual", "prices"), annual_fields=("eps", "pat"), min_annual_years=2
    ),
    # quality
    "roe": Need(("annual",), annual_fields=("pat", "total_equity")),
    "roce": Need(("annual",), annual_fields=("profit_before_tax", "total_equity")),
    "debt_to_equity": Need(("annual",), annual_fields=("total_debt", "total_equity")),
    "interest_coverage": Need(
        ("annual",), annual_fields=("profit_before_tax", "interest_expense")
    ),
    "cfo_to_pat": Need(("annual",), annual_fields=("ocf", "pat")),
    "accruals": Need(("annual",), annual_fields=("pat", "ocf", "total_assets")),
    # growth
    "revenue_cagr_3y": Need(("annual",), annual_fields=("revenue",), min_annual_years=2),
    "pat_cagr_3y": Need(("annual",), annual_fields=("pat",), min_annual_years=2),
    "margin_trend": Need(
        ("annual",), annual_fields=("pat", "revenue"), min_annual_years=2
    ),
    "quarterly_revenue_yoy": Need(
        ("quarterly",), quarterly_fields=("revenue",), min_quarters=5
    ),
    "quarterly_pat_yoy": Need(("quarterly",), quarterly_fields=("pat",), min_quarters=5),
    # momentum
    "momentum_12_1": Need(("prices",), min_price_rows=180),
    "price_to_200dma": Need(("prices",), min_price_rows=150),
    "relative_strength_6m": Need(("prices",), min_price_rows=90),
    # sentiment
    "news_sentiment_30d": Need(("news",), min_articles=sentiment.MIN_ARTICLES),
}

# Which datasets each red flag needs to reach a verdict other than UNKNOWN.
# `reachable=False` flags are omitted: no dataset in the system supplies them,
# so listing one would imply a step that would clear them.
FLAG_DATASETS: dict[str, tuple[str, ...]] = {
    "auditor_qualification": ("annual",),
    "weak_cash_conversion": ("annual",),
    "promoter_selling": ("shareholding",),
    "contingent_liabilities": ("annual",),
}


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SourceStatus:
    key: str
    label: str
    have: Have
    detail: str          # what we hold, in one line
    gap: str             # what is missing, empty when PRESENT
    step: str            # pipeline step key that fills it
    blocks: tuple[str, ...] = ()   # factor names this dataset gates

    @property
    def spec(self) -> DatasetSpec:
        return DATASETS_BY_KEY[self.key]


@dataclass(frozen=True)
class FactorStatus:
    name: str
    family: str
    weight: float               # share of the whole model, 0-1
    value: float | None
    blocked_by: tuple[str, ...] # dataset keys, empty when computable or n/a
    reason: str                 # empty when computable

    @property
    def computable(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class FamilyStatus:
    family: str
    weight: float
    measured: int
    total: int
    covered: float              # fraction of the family's own weight measured


@dataclass(frozen=True)
class FlagStatus:
    name: str
    state: str                  # redflags.FlagState value
    description: str
    reachable: bool
    blocked_by: tuple[str, ...]


@dataclass(frozen=True)
class Readiness:
    isin: str
    symbol: str | None
    name: str
    sector: str | None
    as_of: dt.date
    index_name: str
    in_universe: bool

    coverage: float
    min_coverage: float
    sources: tuple[SourceStatus, ...]
    factors: tuple[FactorStatus, ...]
    families: tuple[FamilyStatus, ...]
    flags: tuple[FlagStatus, ...]

    # The last persisted evaluation, not one computed here. See module docstring.
    stored_as_of: dt.date | None = None
    stored_score: float | None = None
    stored_signal: str | None = None
    stored_version: str | None = None
    stale_signal: bool = False

    @property
    def scorable(self) -> bool:
        """Whether a universe run today would produce a score for this company."""
        return self.in_universe and self.coverage >= self.min_coverage

    @property
    def gaps(self) -> tuple[SourceStatus, ...]:
        return tuple(s for s in self.sources if s.have is not Have.PRESENT)

    def next_steps(self) -> tuple[str, ...]:
        """Pipeline step keys that would close the gaps, in dependency order.

        Registry order, not gap order: extraction after a filing download is the
        only sequence that works, and a caller sorting by "biggest gap first"
        would send Claude to read PDFs that have not been fetched.
        """
        from stockanalysis.run.steps import STEPS

        wanted = {s.step for s in self.gaps}
        # Extraction is meaningless without the PDFs, and the PDFs are a gap
        # only until they are downloaded — so a run that extracts must fetch.
        if "extract" in wanted:
            wanted.add("filings")
        return tuple(s.key for s in STEPS if s.key in wanted)


# ----------------------------------------------------------------------


def readiness(
    db: Database,
    isin: str,
    as_of: dt.date | None = None,
    index_name: str | None = None,
    config: ScoringConfig | None = None,
) -> Readiness:
    """Assemble the readiness report for one company.

    Everything computed here is per-company by construction. The factor values
    come from a one-company scoring pass, which is exact for `raw`, `coverage`
    and the red flags — none of those depend on the rest of the universe — and
    meaningless for the score, which is why the score is not read from it.
    """
    as_of = as_of or dt.date.today()
    index_name = index_name or settings.default_index
    cfg = config or ScoringConfig()

    meta = db.query(
        "SELECT isin, nse_symbol, name, sector FROM instruments WHERE isin = ?", [isin]
    )
    if meta.empty:
        raise ValueError(
            f"{isin} is not in `instruments`. Seed the universe first "
            f"(`stockanalysis seed-universe`)."
        )
    row = meta.iloc[0]

    panel = PANEL_CACHE.get(db, [isin], as_of)
    result = CompositeModel(config=cfg, cache=PANEL_CACHE).score(db, [isin], as_of)

    sources = _sources(db, isin, as_of, panel)
    have = {s.key: s.have for s in sources}

    factors = _factors(result, isin, have, panel)
    sources = _attach_blocks(sources, factors)

    return Readiness(
        isin=isin,
        symbol=row["nse_symbol"],
        name=row["name"],
        sector=row["sector"],
        as_of=as_of,
        index_name=index_name,
        in_universe=isin in db.as_of_universe(index_name, as_of),
        coverage=float(result.coverage.get(isin, 0.0) or 0.0),
        min_coverage=cfg.min_coverage,
        sources=sources,
        factors=factors,
        families=_families(factors),
        flags=_flags(result, isin, have),
        **_stored_signal(db, isin, as_of),
    )


def resolve(db: Database, symbol: str) -> str | None:
    """NSE symbol or ISIN to ISIN. Case-insensitive, returns None if unknown."""
    target = symbol.strip().upper()
    df = db.query(
        "SELECT isin FROM instruments WHERE UPPER(nse_symbol) = ? OR UPPER(isin) = ?",
        [target, target],
    )
    return None if df.empty else str(df["isin"].iloc[0])


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------


def _sources(
    db: Database, isin: str, as_of: dt.date, panel: Panel
) -> tuple[SourceStatus, ...]:
    return (
        _prices_status(db, isin, as_of),
        _quarterly_status(panel),
        _shareholding_status(panel),
        _filings_status(db, isin),
        _annual_status(panel),
        _news_status(db, isin, as_of, panel),
    )


def _prices_status(db: Database, isin: str, as_of: dt.date) -> SourceStatus:
    px = db.as_of_prices([isin], as_of, lookback_days=400)
    if px.empty:
        return SourceStatus(
            "prices", "Daily prices", Have.ABSENT,
            "nothing in the year to the decision date",
            "momentum needs ~180 trading days; every market-cap ratio needs a price",
            "prices",
        )

    dates = pd.to_datetime(px["date"])
    first, last = dates.min().date(), dates.max().date()
    n = len(px)
    detail = f"{n} trading days, {first} → {last}"

    staleness = (as_of - last).days
    if staleness > PRICE_STALE_DAYS:
        return SourceStatus(
            "prices", "Daily prices", Have.PARTIAL, detail,
            f"last close is {staleness} days before the decision date — beyond "
            f"the {PRICE_STALE_DAYS}-day tolerance, so the panel carries no price",
            "prices",
        )
    if n < 180:
        return SourceStatus(
            "prices", "Daily prices", Have.PARTIAL, detail,
            f"{n} of the ~180 days 12-1 momentum needs", "prices",
        )
    return SourceStatus("prices", "Daily prices", Have.PRESENT, detail, "", "prices")


def _quarterly_status(panel: Panel) -> SourceStatus:
    rows = _mine(panel.quarterly, panel.isins[0])
    if rows.empty:
        return SourceStatus(
            "quarterly", "Quarterly results", Have.ABSENT, "none",
            "the two quarterly growth factors are the only fundamentals that "
            "need no LLM — this is the cheapest coverage available",
            "quarterly",
        )
    latest = pd.to_datetime(rows["period_end_date"]).max().date()
    detail = f"{len(rows)} quarters, latest {latest}"
    if len(rows) < 5:
        return SourceStatus(
            "quarterly", "Quarterly results", Have.PARTIAL, detail,
            f"{len(rows)} of the 5 quarters a year-on-year comparison needs",
            "quarterly",
        )
    behind = (panel.as_of - latest).days
    if behind > STALE_PERIOD_DAYS:
        return SourceStatus(
            "quarterly", "Quarterly results", Have.PARTIAL, detail,
            f"newest quarter ended {behind} days ago — roughly "
            f"{behind // 91} quarters behind, so the growth factors compute "
            f"off history and nothing downstream flags it",
            "quarterly",
        )
    return SourceStatus(
        "quarterly", "Quarterly results", Have.PRESENT, detail, "", "quarterly"
    )


def _shareholding_status(panel: Panel) -> SourceStatus:
    rows = _mine(panel.shareholding, panel.isins[0])
    if rows.empty:
        return SourceStatus(
            "shareholding", "Shareholding pattern", Have.ABSENT, "none",
            "the promoter-selling red flag cannot be evaluated", "shareholding",
        )
    latest = pd.to_datetime(rows["quarter_end"]).max().date()
    detail = f"{len(rows)} quarters, latest {latest}"
    if len(rows) < 4:
        return SourceStatus(
            "shareholding", "Shareholding pattern", Have.PARTIAL, detail,
            f"{len(rows)} of the 4 quarters needed to see three consecutive "
            f"declines", "shareholding",
        )
    behind = (panel.as_of - latest).days
    if behind > STALE_PERIOD_DAYS:
        return SourceStatus(
            "shareholding", "Shareholding pattern", Have.PARTIAL, detail,
            f"newest quarter ended {behind} days ago — the promoter-selling "
            f"flag would read a stale trend as a current one",
            "shareholding",
        )
    return SourceStatus(
        "shareholding", "Shareholding pattern", Have.PRESENT, detail, "", "shareholding"
    )


def _filings_status(db: Database, isin: str) -> SourceStatus:
    """Downloaded PDFs, and how many of them have made it into the schema.

    Not filtered by knowledge date. A PDF on disk is a fact about the filesystem
    — it is either there to extract or it is not — and hiding one because its
    broadcast date falls after `as_of` would report a download as still needed.
    """
    df = db.query(
        """
        SELECT f.fiscal_year,
               (SELECT COUNT(*) FROM fundamentals_annual a
                 WHERE a.source_filing_id = f.filing_id) AS extracted
        FROM filings f
        WHERE f.isin = ? AND f.doc_type = 'ANNUAL_REPORT'
        ORDER BY f.fiscal_year DESC
        """,
        [isin],
    )
    if df.empty:
        return SourceStatus(
            "filings", "Annual report PDFs", Have.ABSENT, "none downloaded",
            f"nothing for extraction to read; {settings.filing_years} years is "
            f"the configured target", "filings",
        )

    years = [int(y) for y in df["fiscal_year"].dropna()]
    done = int((df["extracted"] > 0).sum())
    span = ", ".join(f"FY{y}" for y in years) if years else f"{len(df)} reports"
    detail = f"{len(df)} downloaded ({span}), {done} extracted"

    if len(df) < settings.filing_years:
        return SourceStatus(
            "filings", "Annual report PDFs", Have.PARTIAL, detail,
            f"{len(df)} of the {settings.filing_years} years configured — a "
            f"3-year CAGR needs three reports", "filings",
        )
    return SourceStatus(
        "filings", "Annual report PDFs", Have.PRESENT, detail, "", "filings"
    )


def _annual_status(panel: Panel) -> SourceStatus:
    """Extracted annual financials, as the panel is allowed to see them."""
    rows = _mine(panel.annual, panel.isins[0])
    if rows.empty:
        return SourceStatus(
            "annual", "Extracted annual financials", Have.ABSENT,
            "none visible on the decision date",
            "the value, quality and growth families are ~75% of the model and "
            "are built from this",
            "extract",
        )

    years = sorted(
        {pd.Timestamp(d).year for d in rows["period_end_date"].dropna()}, reverse=True
    )
    basis = str(rows["basis"].iloc[0]) if "basis" in rows else "?"
    confidence = pd.to_numeric(
        rows.get("extraction_confidence", pd.Series(dtype=float)), errors="coerce"
    )
    conf_note = (
        f", confidence {confidence.min():.2f}–{confidence.max():.2f}"
        if confidence.notna().any()
        else ""
    )
    detail = (
        f"{len(rows)} year(s) — {', '.join(str(y) for y in years)} "
        f"({basis}{conf_note})"
    )

    if len(rows) < 3:
        return SourceStatus(
            "annual", "Extracted annual financials", Have.PARTIAL, detail,
            f"{len(rows)} of the 3 years the CAGR and cash-conversion factors "
            f"need", "extract",
        )
    return SourceStatus(
        "annual", "Extracted annual financials", Have.PRESENT, detail, "", "extract"
    )


def _news_status(
    db: Database, isin: str, as_of: dt.date, panel: Panel
) -> SourceStatus:
    """Scored articles inside the sentiment window, plus what is stored but unscored.

    Stored-and-unscored is a different gap from stored-nothing: one is a free
    local model away, the other needs an ingest. Reporting them as one number
    would send an operator to fetch news they already have.
    """
    scored = len(_mine(panel.sentiment, isin))
    window = 30
    start = as_of - dt.timedelta(days=window)
    total = db.query(
        "SELECT COUNT(*) AS c FROM news WHERE isin = ? "
        "AND published_at >= ? AND published_at <= ?",
        [isin, start, dt.datetime.combine(as_of, dt.time.max)],
    )["c"].iloc[0]
    total = int(total)

    if total == 0:
        return SourceStatus(
            "news", "Scored news", Have.ABSENT,
            f"no articles in the {window} days to {as_of}",
            "the sentiment family is 10% of the model and stays unmeasured",
            "news",
        )

    detail = f"{total} article(s) in the {window}-day window, {scored} scored"
    if scored < sentiment.MIN_ARTICLES:
        step = "sentiment" if scored < total else "news"
        gap = (
            f"{total - scored} stored but unscored — FinBERT is local and free"
            if scored < total
            else f"{scored} scored, below the {sentiment.MIN_ARTICLES}-article "
                 f"floor the factor requires"
        )
        return SourceStatus("news", "Scored news", Have.PARTIAL, detail, gap, step)
    return SourceStatus("news", "Scored news", Have.PRESENT, detail, "", "sentiment")


def _mine(df: pd.DataFrame, isin: str) -> pd.DataFrame:
    """The rows of a panel frame belonging to one ISIN.

    Panel frames are keyed by `isin` as a column, except `latest`, which is
    indexed by it. Only column-keyed frames reach here.
    """
    if df.empty or "isin" not in df.columns:
        return df.iloc[0:0] if isinstance(df, pd.DataFrame) else pd.DataFrame()
    return df[df["isin"] == isin]


# ----------------------------------------------------------------------
# Factors
# ----------------------------------------------------------------------


def _factors(
    result, isin: str, have: dict[str, Have], panel: Panel
) -> tuple[FactorStatus, ...]:
    all_factors = default_factors()
    intra = intra_family_weights(all_factors)
    family_totals = {
        fam: sum(intra[f.name] for f in all_factors if f.family == fam)
        for fam in {f.family for f in all_factors}
    }

    out = []
    for f in all_factors:
        raw = result.raw[f.name].get(isin) if f.name in result.raw.columns else None
        value = None if raw is None or pd.isna(raw) else float(raw)
        share = FAMILY_WEIGHTS.get(f.family, 0.0) * (
            intra[f.name] / family_totals[f.family] if family_totals[f.family] else 0.0
        )

        blocked, reason = ((), "")
        if value is None:
            blocked, reason = _diagnose(f.name, have, panel, isin)

        out.append(
            FactorStatus(
                name=f.name,
                family=f.family,
                weight=share,
                value=value,
                blocked_by=blocked,
                reason=reason,
            )
        )
    return tuple(out)


def _diagnose(
    name: str, have: dict[str, Have], panel: Panel, isin: str
) -> tuple[tuple[str, ...], str]:
    """Why this factor came back NaN, and which datasets would change that.

    Checked cheapest-cause-first: a missing dataset explains everything
    downstream of it, so reporting "EPS is null" for a company with no extracted
    report at all would be true and useless.
    """
    need = NEEDS.get(name)
    if need is None:
        return (), "no requirement declared — see readiness.NEEDS"

    missing = tuple(k for k in need.datasets if have.get(k) is Have.ABSENT)
    if missing:
        labels = ", ".join(DATASETS_BY_KEY[k].label.lower() for k in missing)
        return missing, f"no {labels}"

    annual = _mine(panel.annual, isin)
    if need.min_annual_years > 1 and len(annual) < need.min_annual_years:
        return ("annual",), (
            f"{len(annual)} annual report(s), needs {need.min_annual_years}"
        )

    null_fields = [
        c for c in need.annual_fields
        if annual.empty
        or c not in annual.columns
        or pd.isna(pd.to_numeric(annual[c], errors="coerce").iloc[0])
    ]
    if null_fields:
        return ("annual",), (
            f"extracted, but {', '.join(null_fields)} not populated in the "
            f"latest report"
        )

    quarterly = _mine(panel.quarterly, isin)
    if need.min_quarters and len(quarterly) < need.min_quarters:
        return ("quarterly",), (
            f"{len(quarterly)} quarter(s), needs {need.min_quarters} to reach "
            f"the same quarter a year earlier"
        )

    if need.min_articles and len(_mine(panel.sentiment, isin)) < need.min_articles:
        return ("news",), (
            f"fewer than {need.min_articles} scored articles in the window"
        )

    # Every input is present and the factor still declined to produce a number.
    # That is the factor working: a negative denominator, a loss-making base, a
    # CAGR across a sign change. No amount of ingest changes it.
    return (), (
        "inputs present — the ratio is undefined for this company (negative or "
        "zero base; see the factor's docstring)"
    )


def _families(factors: tuple[FactorStatus, ...]) -> tuple[FamilyStatus, ...]:
    out = []
    for family, weight in FAMILY_WEIGHTS.items():
        members = [f for f in factors if f.family == family]
        if not members:
            continue
        total_w = sum(f.weight for f in members)
        measured_w = sum(f.weight for f in members if f.computable)
        out.append(
            FamilyStatus(
                family=family,
                weight=weight,
                measured=sum(1 for f in members if f.computable),
                total=len(members),
                covered=measured_w / total_w if total_w else 0.0,
            )
        )
    return tuple(out)


def _attach_blocks(
    sources: tuple[SourceStatus, ...], factors: tuple[FactorStatus, ...]
) -> tuple[SourceStatus, ...]:
    """Label each gap with the factors actually blocked on it, not the ones that
    merely mention it.

    Declared dependencies would say a missing annual report blocks eleven
    factors. Observed ones say it blocks the eleven that came back NaN — which
    excludes any that a fallback rescued, and is the number worth acting on.
    """
    out = []
    for source in sources:
        blocked = tuple(
            f.name for f in factors if source.key in f.blocked_by
        )
        out.append(
            SourceStatus(
                source.key, source.label, source.have, source.detail,
                source.gap, source.step, blocked,
            )
        )
    return tuple(out)


def _flags(result, isin: str, have: dict[str, Have]) -> tuple[FlagStatus, ...]:
    states = result.flags.loc[isin] if isin in result.flags.index else {}
    out = []
    for definition in redflags.DEFINITIONS:
        state = str(states.get(definition.name, redflags.FlagState.UNKNOWN.value))
        blocked: tuple[str, ...] = ()
        if state == redflags.FlagState.UNKNOWN.value and definition.reachable:
            blocked = tuple(
                k for k in FLAG_DATASETS.get(definition.name, ())
                if have.get(k) is not Have.PRESENT
            )
        out.append(
            FlagStatus(
                name=definition.name,
                state=state,
                description=definition.description,
                reachable=definition.reachable,
                blocked_by=blocked,
            )
        )
    return tuple(out)


def _stored_signal(db: Database, isin: str, as_of: dt.date) -> dict:
    """The most recent persisted signal at or before `as_of`.

    At or before, never the latest outright: a report for a decision date in the
    past that showed a signal computed after it would be describing information
    the model was not allowed to have.
    """
    df = db.query(
        "SELECT as_of_date, composite_score, signal, model_version FROM signals "
        "WHERE isin = ? AND as_of_date <= ? ORDER BY as_of_date DESC LIMIT 1",
        [isin, as_of],
    )
    if df.empty:
        return {}
    row = df.iloc[0]
    stored_as_of = pd.Timestamp(row["as_of_date"]).date()
    score = row["composite_score"]
    return {
        "stored_as_of": stored_as_of,
        "stored_score": None if pd.isna(score) else float(score),
        "stored_signal": None if pd.isna(row["signal"]) else str(row["signal"]),
        "stored_version": None if pd.isna(row["model_version"]) else str(
            row["model_version"]
        ),
        "stale_signal": stored_as_of < as_of,
    }
