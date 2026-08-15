"""The pipeline steps, declared so a UI can show them before running them.

Each step is a thin wrapper over the function its CLI command already calls.
The wrapper's job is to report — how many rows, which company is in flight,
what confidence each extraction came back with — not to do the work.

Three properties this file is built to preserve:

- **Nothing is hidden.** A step that cannot run (no API key, torch not
  installed, nothing pending) reports SKIPPED with the reason. It never reports
  success for work it did not do, because "green" that means "did nothing" is
  worse than a visible gap.
- **Cost is declared up front.** `StepSpec.cost` is what the UI reads to decide
  which steps to leave switched off by default and which to put a price warning
  next to. Only two steps spend money, and both are off unless asked for.
- **Steps are ordered by dependency.** Extraction needs downloaded filings;
  scoring needs prices. `Plan` keeps registry order for exactly that reason.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.run.events import Reporter

# What a step costs to run. The UI turns this into a badge and a default.
FREE = "free"          # local only
NETWORK = "network"    # unofficial NSE/Yahoo endpoints — rate-limited, slow
PAID = "paid"          # spends money per call


class StepSkipped(Exception):
    """Raised by a step that cannot or need not run. Not a failure."""


@dataclass
class StepContext:
    """Everything a step is allowed to touch."""

    db: Database
    report: Reporter
    # None means "the whole seeded universe" — the same convention the ingest
    # functions already use for `isins`.
    isins: list[str] | None = None
    symbol: str | None = None
    company: str | None = None
    options: RunOptions = field(default_factory=lambda: RunOptions())

    @property
    def scope_label(self) -> str:
        return self.symbol or self.company or "the universe"


@dataclass
class RunOptions:
    """Knobs the operator can turn before starting a job."""

    index_name: str = settings.default_index
    price_years: int = 6
    filing_years: int = settings.filing_years
    extraction_model: str = settings.extraction_model
    extraction_limit: int = 3
    redo_extraction: bool = False
    as_of: dt.date | None = None          # None -> today, resolved at run time
    min_coverage: float = 0.5
    apply_red_flags: bool = True
    sentiment_limit: int | None = 500
    marketaux_max_requests: int = 5

    def decision_date(self) -> dt.date:
        return self.as_of or dt.date.today()


StepFn = Callable[[StepContext], None]


@dataclass(frozen=True)
class StepSpec:
    key: str
    label: str
    description: str
    run: StepFn
    cost: str = FREE
    # Off by default means "costs money, or takes hours" — never "unimportant".
    default_on: bool = True
    # "company" steps are meaningless for a universe run and vice versa.
    scopes: tuple[str, ...] = ("company", "universe")


# ----------------------------------------------------------------------
# Steps
# ----------------------------------------------------------------------


def _resolve(ctx: StepContext) -> None:
    """Turn the typed symbol into an instrument. No network, always first.

    Also the step that decides `ctx.isins` for everything after it, which is
    why a failure here stops the job rather than skipping: every later step
    would otherwise silently run against the whole universe.
    """
    target = (ctx.symbol or "").strip().upper()
    if not target:
        raise ValueError("No company given.")

    df = ctx.db.query(
        "SELECT isin, nse_symbol, name, sector, industry, is_active "
        "FROM instruments WHERE UPPER(nse_symbol) = ? OR UPPER(isin) = ?",
        [target, target],
    )
    if df.empty:
        raise ValueError(
            f"{target} is not in `instruments`. Seed the universe first "
            f"(`stockanalysis seed-universe`), or check the NSE symbol."
        )

    row = df.iloc[0]
    ctx.isins = [row["isin"]]
    ctx.symbol = row["nse_symbol"]
    ctx.company = row["name"]

    ctx.report.summary(
        ISIN=row["isin"],
        Symbol=row["nse_symbol"],
        Sector=row["sector"] or "—",
    )
    ctx.report.log(
        f"{row['name']} ({row['nse_symbol']}) — {row['sector'] or 'no sector'}",
        isin=row["isin"],
    )
    if not bool(row["is_active"]):
        ctx.report.warn("This instrument is marked delisted/inactive.")


def _seed_universe(ctx: StepContext) -> None:
    """Refresh index membership from NSE."""
    from stockanalysis.universe.loader import seed_index_from_nse

    index_name = ctx.options.index_name
    ctx.report.log(f"Fetching {index_name} constituents from NSE...")
    n = seed_index_from_nse(ctx.db, index_name)
    ctx.report.summary(**{"instruments seeded": n, "index": index_name})
    ctx.report.log(f"{n} instruments in {index_name}")


def _prices(ctx: StepContext) -> None:
    """Daily OHLCV via yfinance, adjusted close included."""
    from stockanalysis.ingest.prices import ingest_prices

    end = dt.date.today()
    start = end - dt.timedelta(days=365 * ctx.options.price_years)
    ctx.report.log(f"Fetching {ctx.options.price_years}y of prices ({start} to {end})")

    n = ingest_prices(
        ctx.db,
        isins=ctx.isins,
        start=start,
        end=end,
        progress=_company_progress(ctx, "fetching prices for"),
    )
    ctx.report.summary(**{"price rows stored": f"{n:,}"})

    # A row count says the call worked; the date range says whether the data is
    # usable. A series that stops six months ago scores today's momentum off
    # stale prices, and nothing downstream would complain.
    stats = _price_stats(ctx.db, ctx.isins)
    if stats is not None:
        ctx.report.summary(**{"covering": f"{stats['first']} → {stats['last']}"})
        staleness = (dt.date.today() - stats["last"]).days
        if staleness > 7:
            ctx.report.warn(
                f"Latest price is {staleness} days old. Momentum and value "
                f"factors will be computed off stale prices.",
                last_date=str(stats["last"]),
            )
    if n == 0:
        raise StepSkipped(
            "yfinance returned nothing. The symbol may be wrong, or the "
            "unofficial endpoint may be rate-limiting."
        )


def _quarterly(ctx: StepContext) -> None:
    """NSE quarterly results — free, and the cross-check LLM extraction is scored against."""
    from stockanalysis.ingest.nse_fundamentals import ingest_quarterly

    n = ingest_quarterly(
        ctx.db, isins=ctx.isins, progress=_company_progress(ctx, "results_comparison for")
    )
    ctx.report.summary(**{"quarterly rows stored": n})
    if n == 0:
        raise StepSkipped("NSE returned no quarterly results for this company.")

    latest = _latest_quarter(ctx.db, ctx.isins)
    if latest is not None:
        ctx.report.summary(**{"latest quarter": str(latest)})
    ctx.report.log(
        f"{n} quarterly rows. Amounts are in ₹ lakhs as NSE publishes them; "
        f"the validator converts before comparing."
    )


def _shareholding(ctx: StepContext) -> None:
    """Quarterly shareholding pattern — promoter holding trend and the red flag it feeds."""
    from stockanalysis.ingest.shareholding import ingest_shareholding

    n = ingest_shareholding(
        ctx.db, isins=ctx.isins, progress=_company_progress(ctx, "shareholding for")
    )
    ctx.report.summary(**{"shareholding rows stored": n})
    if n == 0:
        raise StepSkipped("NSE returned no shareholding pattern for this company.")

    if ctx.isins:
        df = ctx.db.query(
            "SELECT quarter_end, promoter_pct, promoter_pledged_pct, fii_pct, dii_pct "
            "FROM shareholding WHERE isin = ? ORDER BY quarter_end DESC LIMIT 6",
            [ctx.isins[0]],
        )
        for r in df.itertuples(index=False):
            ctx.report.row(
                quarter=str(r.quarter_end),
                promoter_pct=_num(r.promoter_pct),
                pledged_pct=_num(r.promoter_pledged_pct),
                fii_pct=_num(r.fii_pct),
                dii_pct=_num(r.dii_pct),
            )
    ctx.report.warn(
        "Promoter *pledge* is not in this feed — `promoter_pledged_pct` stays "
        "NULL and must be read as unknown, never as zero. The pledge red flag "
        "cannot be evaluated from this source."
    )


def _filings(ctx: StepContext) -> None:
    """Download annual-report PDFs from NSE."""
    from stockanalysis.ingest.filings import fetch_annual_reports

    ctx.report.log(
        f"Listing up to {ctx.options.filing_years} years of annual reports "
        f"(~{settings.request_delay_seconds}s between requests)"
    )

    def document(record: dict) -> None:
        mb = (record.get("bytes") or 0) / 1_000_000
        ctx.report.row(
            fiscal_year=record.get("fiscal_year"),
            pages=record.get("page_count"),
            size_mb=round(mb, 1),
            knowledge_date=str(record.get("broadcast_date")),
            date_source=record.get("broadcast_date_source"),
        )
        ctx.report.log(
            f"FY{record.get('fiscal_year')} — {record.get('page_count')} pages, "
            f"{mb:.1f} MB",
            fiscal_year=record.get("fiscal_year"),
        )

    n = fetch_annual_reports(
        ctx.db,
        isins=ctx.isins,
        years=ctx.options.filing_years,
        progress=_company_progress(ctx, "listing reports for"),
        on_document=document,
    )
    ctx.report.summary(**{"filings registered": n})
    if n == 0:
        raise StepSkipped(
            "No annual reports found. NSE lists them by symbol and does not "
            "have them for every company."
        )

    assumed = [r for r in _step_rows(ctx) if r.get("date_source") != "NSE"]
    if assumed:
        ctx.report.warn(
            f"{len(assumed)} report(s) have an assumed knowledge date (period "
            f"end + 6 months, the statutory AGM deadline). Deliberately late — "
            f"a backtest will understate signal rather than fabricate it."
        )


def _extract(ctx: StepContext) -> None:
    """Claude reads the PDFs into the schema, and the validators score the result.

    The only step whose output is a *judgement* rather than a fetch, which is
    why every filing gets its confidence and the checks behind it reported
    individually rather than averaged into one number.
    """
    from stockanalysis.extract.factory import make_extractor
    from stockanalysis.extract.pipeline import pending_filings, run_extraction

    model = ctx.options.extraction_model
    filings = pending_filings(
        ctx.db,
        limit=ctx.options.extraction_limit,
        isins=ctx.isins,
        only_unextracted=not ctx.options.redo_extraction,
        model=model,
    )
    if not filings:
        raise StepSkipped(
            "Nothing pending — every downloaded report has already been "
            "extracted with this model. Tick 're-extract' to run them again."
        )

    ctx.report.log(f"Extracting {len(filings)} filing(s) with {model}")
    extractor = make_extractor(model)

    def progress(i: int, n: int, filing, result, report) -> None:
        confidence = report.confidence if report else 0.0
        ctx.report.row(
            fiscal_year=filing.fiscal_year,
            confidence=confidence,
            verdict=_confidence_verdict(confidence, result.error),
            failed_checks=(
                "; ".join(c.name for c in report.hard_failures + report.soft_failures)
                if report else ""
            ),
            cost_usd=round(result.cost_usd(), 3),
            seconds=round(result.latency_seconds, 1),
            error=result.error or "",
        )
        if result.error:
            ctx.report.error(
                f"FY{filing.fiscal_year} failed: {result.error}",
                fiscal_year=filing.fiscal_year,
            )
        else:
            level = "info" if confidence >= 1.0 else "warn"
            ctx.report.log(
                f"FY{filing.fiscal_year} extracted — confidence {confidence} "
                f"({_confidence_verdict(confidence, None)}), "
                f"${result.cost_usd():.3f}, {result.latency_seconds:.0f}s",
                level=level,
                fiscal_year=filing.fiscal_year,
                confidence=confidence,
            )
            if report:
                for check in report.hard_failures + report.soft_failures:
                    ctx.report.warn(
                        f"FY{filing.fiscal_year} {check.name} failed "
                        f"({check.severity}): {check.detail}"
                    )
        ctx.report.progress(i, n, f"{i}/{n} filings")
        # Between filings is the only safe place to stop: a filing already sent
        # to the model has been paid for either way, so it is finished and
        # persisted rather than abandoned.
        ctx.report.check_cancelled()

    results = run_extraction(
        ctx.db, filings, extractor, run_label="ui", progress=progress
    )

    ok = sum(1 for r, _ in results if r.ok)
    clean = sum(1 for _, rep in results if rep and rep.confidence >= 1.0)
    queued = sum(1 for _, rep in results if rep and rep.confidence < 0.6)
    cost = sum(r.cost_usd() for r, _ in results)
    ctx.report.summary(**{
        "extracted": f"{ok}/{len(results)}",
        "passed every validator": clean,
        "cost": f"${cost:.2f}",
    })
    if queued:
        ctx.report.warn(
            f"{queued} extraction(s) scored below 0.6 and went to the human "
            f"review queue rather than into `fundamentals_annual`."
        )
    if ok == 0:
        raise RuntimeError("Every extraction failed — see the log above.")


def _news_rss(ctx: StepContext) -> None:
    """RSS headlines. Free and unlimited, but the feeds are exchange-wide.

    There is no per-company RSS: this fetches each feed's current front page and
    resolves whatever companies are named. Running it during a single-company
    update is still worth it — it is the only free news path — but it will store
    articles about other companies too, and it cannot fill in history.
    """
    from stockanalysis.ingest.rss import ingest_rss
    from stockanalysis.news.resolve import EmptyAliasTableError

    try:
        stats, per_feed = ingest_rss(ctx.db)
    except EmptyAliasTableError as e:
        raise StepSkipped(f"{e}") from e

    for url, n in per_feed.items():
        feed = url.split("/")[2] if "/" in url else url
        if n < 0:
            ctx.report.warn(f"{feed}: fetch failed")
        else:
            ctx.report.row(feed=feed, items=n)

    ctx.report.summary(**{
        "articles fetched": stats.fetched,
        "rows stored": stats.stored,
        "resolved to a company": stats.resolved,
    })
    ctx.report.log(
        f"{stats.resolution_rate:.0%} of articles named a company in the "
        f"universe; the rest are index and macro stories."
    )
    if ctx.isins:
        mine = ctx.db.query(
            "SELECT COUNT(*) AS c FROM news WHERE isin = ? "
            "AND ingested_at >= CURRENT_DATE",
            [ctx.isins[0]],
        )["c"].iloc[0]
        ctx.report.summary(**{f"about {ctx.scope_label}": int(mine)})


def _news_marketaux(ctx: StepContext) -> None:
    """Entity-tagged news — Marketaux knows which ticker an article is about."""
    from stockanalysis.ingest.marketaux import MarketauxUnavailableError, ingest_marketaux

    try:
        stats = ingest_marketaux(
            ctx.db,
            isins=ctx.isins,
            max_requests=ctx.options.marketaux_max_requests,
        )
    except MarketauxUnavailableError as e:
        raise StepSkipped(f"{e}") from e

    ctx.report.summary(**{"rows stored": stats.stored})
    ctx.report.log(str(stats))


def _sentiment(ctx: StepContext) -> None:
    """FinBERT over unscored headlines. Local, free, CPU.

    Scores the whole pending queue, not only this company's rows — the model
    loads once and the marginal cost per headline is negligible, so restricting
    it would mean loading a 110M-parameter model to score four articles.
    """
    from stockanalysis.news.finbert import FinBertScorer, ScorerUnavailableError
    from stockanalysis.news.scoring import pending_news, score_news

    outstanding = len(pending_news(ctx.db, settings.sentiment_model))
    if not outstanding:
        raise StepSkipped("Every stored article has already been scored.")

    ctx.report.log(f"Loading {settings.sentiment_model} ({outstanding} rows pending)")
    try:
        scorer = FinBertScorer(model_name=settings.sentiment_model)
    except ScorerUnavailableError as e:
        raise StepSkipped(
            f"{e} Install the sentiment extra: `uv pip install -e '.[sentiment]'`"
        ) from e

    limit = ctx.options.sentiment_limit
    planned = min(limit, outstanding) if limit else outstanding
    ctx.report.log(f"Scoring {planned} rows on {scorer.device}")

    def progress(done: int, total: int) -> None:
        ctx.report.progress(done, total, f"{done}/{total} headlines")

    stats = score_news(ctx.db, scorer, limit=limit, progress=progress)
    ctx.report.summary(**{"rows scored": stats.scored})
    for label, count in sorted(stats.by_label.items()):
        ctx.report.summary(**{label: count})


def _score(ctx: StepContext) -> None:
    """Run the factor model and persist the signal.

    Scoring is cross-sectional by construction — every factor is a sector-
    relative z-score — so this always runs over the whole index universe even
    when the job targets one company. A single company cannot be scored against
    itself, and the UI says so rather than implying the number came from this
    company's data alone.
    """
    from stockanalysis.factors.composite import (
        ScoringConfig,
        family_percentiles,
        persist,
        score_as_of,
    )

    as_of = ctx.options.decision_date()
    cfg = ScoringConfig(
        min_coverage=ctx.options.min_coverage,
        apply_red_flags=ctx.options.apply_red_flags,
    )
    ctx.report.log(
        f"Scoring {ctx.options.index_name} as of {as_of} — sector-relative, "
        f"so the whole universe is scored even for a single-company update."
    )

    result = score_as_of(ctx.db, ctx.options.index_name, as_of, cfg)
    n_factors, n_signals = persist(ctx.db, result)

    table = result.table()
    scored = table[table["signal"].notna()]
    ctx.report.summary(**{
        "universe": len(table),
        "scored": len(scored),
        "factor rows written": n_factors,
        "signals written": n_signals,
    })

    if ctx.isins and ctx.isins[0] in table.index:
        row = table.loc[ctx.isins[0]]
        signal = row["signal"]
        if signal is None or pd.isna(signal):
            ctx.report.warn(
                f"{ctx.scope_label} is unscored: coverage "
                f"{row['coverage']:.0%} is below the {cfg.min_coverage:.0%} "
                f"floor. Unscored is not HOLD — the model could not see enough "
                f"to have an opinion."
            )
            ctx.report.summary(**{"signal": "unscored"})
        else:
            ctx.report.summary(**{
                "signal": str(signal),
                "score": f"{row['score']:.1f}/100",
                "coverage": f"{row['coverage']:.0%}",
                "red flags": row["red_flags"] or "none tripped",
            })
            ctx.report.log(
                f"{ctx.scope_label}: {signal} at {row['score']:.1f}/100 "
                f"(coverage {row['coverage']:.0%})",
                signal=str(signal),
            )

        percentiles = family_percentiles(result)
        if ctx.isins[0] in percentiles.index:
            for family, value in percentiles.loc[ctx.isins[0]].items():
                ctx.report.row(
                    family=family,
                    percentile=None if pd.isna(value) else round(float(value), 1),
                    measured="no — not counted" if pd.isna(value) else "yes",
                )
    elif ctx.isins:
        ctx.report.warn(
            f"{ctx.scope_label} was not in the {ctx.options.index_name} "
            f"universe on {as_of}, so it was not scored. Its data is still "
            f"stored."
        )


def _narrative(ctx: StepContext) -> None:
    """Claude writes the paragraph explaining a score it is not allowed to change."""
    from stockanalysis.serve.narrative import (
        NarrativeGenerator,
        NarrativeUnavailable,
        build_inputs,
    )

    if not ctx.isins:
        raise StepSkipped(
            "Narratives are generated per company. Use the company pipeline, "
            "or `persist(generate_narratives=True)` for a whole universe pass."
        )

    as_of = ctx.options.decision_date()
    row = ctx.db.query(
        "SELECT s.isin, i.nse_symbol, i.name, i.sector, s.composite_score, "
        "       s.signal, s.coverage, s.red_flags, s.unknown_flags "
        "FROM signals s JOIN instruments i ON i.isin = s.isin "
        "WHERE s.isin = ? AND s.as_of_date = ?",
        [ctx.isins[0], as_of],
    )
    if row.empty:
        raise StepSkipped(f"No stored signal for {as_of} — run the scoring step first.")

    r = row.iloc[0]
    if pd.isna(r["composite_score"]):
        raise StepSkipped("Unscored company — there is no rating to explain.")

    item = {
        "isin": r["isin"],
        "nse_symbol": r["nse_symbol"],
        "name": r["name"],
        "sector": r["sector"],
        "composite_score": float(r["composite_score"]),
        "signal": str(r["signal"]),
        "coverage": None if pd.isna(r["coverage"]) else float(r["coverage"]),
        "red_flags": _flags(r["red_flags"]),
        "unknown_flags": _flags(r["unknown_flags"]),
        "family_scores": _stored_family_scores(ctx.db, r["isin"], as_of),
    }

    try:
        written = NarrativeGenerator().generate_many(build_inputs(ctx.db, as_of, [item]))
    except NarrativeUnavailable as e:
        raise StepSkipped(f"{e}") from e

    text = written.get(r["isin"])
    if not text:
        raise StepSkipped("The model returned no text for this company.")

    ctx.db.conn.execute(
        "UPDATE signals SET narrative = ? WHERE isin = ? AND as_of_date = ?",
        [text, r["isin"], as_of],
    )
    ctx.report.summary(**{"narrative": "written"})
    ctx.report.log(text)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _company_progress(ctx: StepContext, verb: str) -> Callable[[int, int, str], None]:
    """Adapt the ingest functions' `(index, total, symbol)` hook to the reporter."""

    def hook(index: int, total: int, symbol: str) -> None:
        # `index` names the company about to be fetched, so completed work is
        # index - 1. For a rate-limited crawl the in-flight name is the useful
        # half of the message.
        ctx.report.progress(index - 1, total, f"{verb} {symbol}")
        if total > 1:
            ctx.report.check_cancelled()

    return hook


def _price_stats(db: Database, isins: list[str] | None) -> dict | None:
    where, params = ("WHERE isin = ?", [isins[0]]) if isins else ("", [])
    df = db.query(
        f"SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS n "
        f"FROM prices_daily {where}",
        params,
    )
    if df.empty or not df["n"].iloc[0]:
        return None
    return {
        "first": pd.Timestamp(df["first"].iloc[0]).date(),
        "last": pd.Timestamp(df["last"].iloc[0]).date(),
        "n": int(df["n"].iloc[0]),
    }


def _latest_quarter(db: Database, isins: list[str] | None) -> dt.date | None:
    if not isins:
        return None
    df = db.query(
        "SELECT MAX(period_end_date) AS q FROM fundamentals_quarterly WHERE isin = ?",
        [isins[0]],
    )
    value = df["q"].iloc[0] if not df.empty else None
    return None if pd.isna(value) else pd.Timestamp(value).date()


def _stored_family_scores(db: Database, isin: str, as_of: dt.date) -> dict[str, float]:
    """Family percentiles reconstructed from stored z-scores.

    Re-scoring here would risk explaining a different number than the one on
    file, which is the failure the explain module exists to avoid.
    """
    from stockanalysis.serve import explain as explain_mod

    try:
        explanation = explain_mod.explain(db, isin, as_of)
    except Exception:  # noqa: BLE001 - an explanation is a nicety, never fatal
        return {}
    if explanation is None:
        return {}
    return {
        c.family: float(c.percentile)
        for c in explanation.families
        if c.percentile is not None
    }


def _confidence_verdict(confidence: float, error: str | None) -> str:
    """DESIGN §5.2's confidence bands, in words."""
    if error:
        return "failed"
    if confidence >= 1.0:
        return "auto-accept"
    if confidence >= 0.6:
        return "flagged"
    return "human review"


def _flags(value: object) -> tuple[str, ...]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ()
    return tuple(p.strip() for p in str(value).split(",") if p.strip())


def _num(value: object) -> float | None:
    return None if value is None or pd.isna(value) else round(float(value), 2)


def _step_rows(ctx: StepContext) -> list[dict]:
    if ctx.report.step_key is None:
        return []
    return ctx.report.job.step(ctx.report.step_key).rows


# ----------------------------------------------------------------------
# Registry — order is dependency order
# ----------------------------------------------------------------------

STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        key="resolve",
        label="Resolve company",
        description="Look the symbol up in `instruments`. Local, instant.",
        run=_resolve,
        cost=FREE,
        scopes=("company",),
    ),
    StepSpec(
        key="seed",
        label="Refresh index membership",
        description="Re-fetch the index constituents from NSE.",
        run=_seed_universe,
        cost=NETWORK,
        default_on=False,
        scopes=("universe",),
    ),
    StepSpec(
        key="prices",
        label="Ingest prices",
        description="Daily OHLCV from yfinance, split/bonus adjusted.",
        run=_prices,
        cost=NETWORK,
    ),
    StepSpec(
        key="quarterly",
        label="Ingest quarterly results",
        description="NSE results_comparison — free, and the LLM cross-check.",
        run=_quarterly,
        cost=NETWORK,
    ),
    StepSpec(
        key="shareholding",
        label="Ingest shareholding",
        description="Promoter / FII / DII holding by quarter.",
        run=_shareholding,
        cost=NETWORK,
    ),
    StepSpec(
        key="filings",
        label="Download annual reports",
        description="Fetch annual-report PDFs from NSE and register them.",
        run=_filings,
        cost=NETWORK,
    ),
    StepSpec(
        key="extract",
        label="Extract financials (Claude)",
        description=(
            "Locate the statements, extract to schema, run the arithmetic "
            "validators, score confidence. Spends money per report."
        ),
        run=_extract,
        cost=PAID,
        default_on=False,
    ),
    StepSpec(
        key="news",
        label="Ingest news (RSS)",
        description="Fetch the configured feeds. Exchange-wide, not per company.",
        run=_news_rss,
        cost=NETWORK,
        default_on=False,
    ),
    StepSpec(
        key="marketaux",
        label="Ingest news (Marketaux)",
        description="Entity-tagged articles. Needs SA_MARKETAUX_API_KEY.",
        run=_news_marketaux,
        cost=NETWORK,
        default_on=False,
    ),
    StepSpec(
        key="sentiment",
        label="Score sentiment (FinBERT)",
        description="Local model over unscored headlines. Free, CPU.",
        run=_sentiment,
        cost=FREE,
        default_on=False,
    ),
    StepSpec(
        key="score",
        label="Score and persist signal",
        description="Factor model, red-flag overlay, BUY/HOLD/SELL.",
        run=_score,
        cost=FREE,
    ),
    StepSpec(
        key="narrative",
        label="Write narrative (Claude)",
        description="A written explanation of the stored score. Costs a few cents.",
        run=_narrative,
        cost=PAID,
        default_on=False,
        scopes=("company",),
    ),
)

STEPS_BY_KEY: dict[str, StepSpec] = {s.key: s for s in STEPS}


@dataclass(frozen=True)
class Plan:
    """What a job will do, decided before it starts."""

    title: str
    scope: str                      # "company" | "universe"
    steps: tuple[StepSpec, ...]
    symbol: str | None = None
    options: RunOptions = field(default_factory=RunOptions)

    def with_options(self, **kwargs: object) -> Plan:
        return replace(self, options=replace(self.options, **kwargs))


def available_steps(scope: str) -> tuple[StepSpec, ...]:
    return tuple(s for s in STEPS if scope in s.scopes)


def _select(scope: str, keys: Sequence[str] | None) -> tuple[StepSpec, ...]:
    """Registry order, always — a caller's ordering is not a dependency order."""
    candidates = available_steps(scope)
    if keys is None:
        return tuple(s for s in candidates if s.default_on)

    wanted = set(keys)
    unknown = wanted - {s.key for s in candidates}
    if unknown:
        raise ValueError(
            f"Unknown step(s) for a {scope} run: {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(s.key for s in candidates)}"
        )
    return tuple(s for s in candidates if s.key in wanted)


def company_plan(
    symbol: str,
    steps: Sequence[str] | None = None,
    options: RunOptions | None = None,
) -> Plan:
    """Update one company end to end.

    `resolve` is always included and always first: every later step depends on
    it to know which ISIN it is working on.
    """
    selected = _select("company", steps)
    if STEPS_BY_KEY["resolve"] not in selected:
        selected = (STEPS_BY_KEY["resolve"], *selected)
    return Plan(
        title=f"Update {symbol.upper()}",
        scope="company",
        steps=selected,
        symbol=symbol.upper(),
        options=options or RunOptions(),
    )


def universe_plan(
    steps: Sequence[str] | None = None, options: RunOptions | None = None
) -> Plan:
    """Update the whole index. Hours, not minutes — every step is a crawl."""
    opts = options or RunOptions()
    return Plan(
        title=f"Update {opts.index_name}",
        scope="universe",
        steps=_select("universe", steps),
        symbol=None,
        options=opts,
    )
