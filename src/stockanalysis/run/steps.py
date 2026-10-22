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
    # Re-reads XBRL filings the pipeline previously refused. Kept under its
    # original name after the paid extraction step was retired, because it is
    # what a parser fix needs to reach the filings it now handles.
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
    """Quarterly revenue, PAT and EPS from the exchange's tagged filings. Free.

    Read from the XBRL instances the filing index links to — the legacy
    `results_comparison` feed has not moved since the December 2024 quarter,
    when results moved to the integrated filing. See `ingest.xbrl_quarterly`
    for why the whole window is re-read rather than only the missing quarters.
    """
    from stockanalysis.ingest.xbrl_quarterly import (
        ingest_quarterly_xbrl,
        pending_quarterly_filings,
    )

    since = dt.date.today() - dt.timedelta(days=365 * ctx.options.filing_years)
    refs = pending_quarterly_filings(
        ctx.db, isins=ctx.isins, since=since,
        only_missing=not ctx.options.redo_extraction,
    )
    if not refs:
        latest = _latest_quarter(ctx.db, ctx.isins)
        if latest is not None:
            ctx.report.summary(**{"latest quarter": str(latest)})
        raise StepSkipped(
            "Every quarterly XBRL filing on file is already ingested."
            if _filing_count(ctx.db, ctx.isins, since)
            else "No results filings on file for this window. Run `Fetch NSE "
                 "filing index` first — quarterly figures are read from the "
                 "XBRL it links to."
        )

    ctx.report.log(f"Reading {len(refs)} tagged quarterly filing(s) from NSE")

    def progress(i: int, n: int, result) -> None:
        ref = result.ref
        row = result.row or {}
        ctx.report.row(
            quarter=str(ref.period_end),
            basis=ref.basis,
            revenue=_num(row.get("revenue")),
            pat=_num(row.get("pat")),
            eps=_num(row.get("eps")),
            filed=str(ref.knowledge_date),
            skipped=result.error or "",
        )
        if result.error:
            ctx.report.warn(f"{ref.period_end} {ref.symbol or ref.isin}: {result.error}")
        ctx.report.progress(i, n, f"{i}/{n} filings")
        ctx.report.check_cancelled()

    results = ingest_quarterly_xbrl(ctx.db, refs, progress=progress)

    stored = sum(1 for r in results if r.ok)
    ctx.report.summary(**{"quarterly rows stored": f"{stored}/{len(results)}"})
    latest = _latest_quarter(ctx.db, ctx.isins)
    if latest is not None:
        ctx.report.summary(**{"latest quarter": str(latest)})
    if stored == 0:
        if any(r.fetch_failed for r in results):
            raise RuntimeError(
                "No filing yielded a usable quarter, and at least one was not "
                "read at all — see the warnings above. That is a fetch failure "
                "rather than a verdict on the filings; re-run it."
            )
        raise StepSkipped(
            "Every tagged filing was read and none carries a usable quarter — "
            "see the warnings above."
        )


def _results_index(ctx: StepContext) -> None:
    """The exchange's filing index: real broadcast dates, and the XBRL links.

    Two indexes, because NSE has two: the legacy financial-results index, which
    stopped at the December 2024 quarter, and the integrated-filing index that
    holds everything since. Both land in `results_filings`, and the two steps
    after this one read nothing but the `xbrl_url` it carries — quarterly
    figures from every filing, annual figures from the year ends.

    Index-wide by date range rather than per company, which is why a
    single-company run still fetches everyone: the endpoints are index-wide, so
    100 companies cost a few dozen requests instead of hundreds.
    """
    from stockanalysis.ingest.nse_fundamentals import ingest_results_index

    end = dt.date.today()
    years = ctx.options.filing_years
    start = end - dt.timedelta(days=365 * years)
    ctx.report.log(
        f"Fetching the NSE results indexes (legacy and integrated) for {start} → "
        f"{end}. A few requests per 90-day window, covering every company on the "
        f"exchange — not just {ctx.scope_label}."
    )

    matched = ingest_results_index(ctx.db, from_date=start, to_date=end)
    ctx.report.summary(**{"filings matched": matched, "window": f"{years}y"})
    if matched == 0:
        raise StepSkipped(
            "NSE returned no filings for this window for any company in the "
            "universe. Both indexes are unofficial endpoints; if this persists, "
            "check the warnings in the log for refused requests."
        )

    linked = _xbrl_link_count(ctx.db, ctx.isins)
    ctx.report.summary(**{"annual filings with XBRL": linked})
    ctx.report.log(
        f"{linked} Q4/annual filing(s) for {ctx.scope_label} carry a tagged "
        f"XBRL instance — those are the ones the annual step reads for free."
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


def _xbrl(ctx: StepContext) -> None:
    """Annual financials from the exchange's own tagged filing. Free.

    The only route to `fundamentals_annual`. The paid path that used to read
    annual-report PDFs with Claude has been retired: the exchange publishes the
    balance sheet and cash flow in tagged form alongside the P&L, so what the
    LLM bought was a second, less reliable reading of figures already available
    for nothing. What is genuinely out of reach this way — a bank's revenue
    line, and years before Ind AS XBRL became routine — is now reported as a
    gap rather than quietly bought.
    """
    from stockanalysis.ingest.xbrl_annual import (
        ingest_annual_xbrl,
        pending_annual_filings,
    )

    # "Redo" covers both annual paths: a filing this step refused is skipped on
    # every later run, which is what stops a bank being offered for ever — but
    # it also means a parser fix would never see the filings it now handles.
    refs = pending_annual_filings(
        ctx.db, isins=ctx.isins, only_missing=not ctx.options.redo_extraction
    )
    if not refs:
        linked = _xbrl_link_count(ctx.db, ctx.isins)
        raise StepSkipped(
            "Every annual XBRL filing on file is already ingested."
            if linked
            else "No XBRL links on file. Run `Fetch NSE filing index` first — "
                 "it is what populates the links this step reads."
        )

    ctx.report.log(f"Reading {len(refs)} tagged annual filing(s) from NSE")

    def progress(i: int, n: int, result) -> None:
        ref = result.ref
        ctx.report.row(
            fiscal_year=ref.fiscal_year,
            basis=ref.basis,
            confidence=result.report.confidence if result.report else 0.0,
            filed=str(ref.broadcast_date),
            skipped=result.error or "",
        )
        if result.error:
            # Not an error in the run: it is the signal that this company-year
            # needs the annual report instead, and the operator should see why.
            ctx.report.warn(f"FY{ref.fiscal_year} {ref.symbol or ref.isin}: {result.error}")
        ctx.report.progress(i, n, f"{i}/{n} filings")
        ctx.report.check_cancelled()

    results = ingest_annual_xbrl(ctx.db, refs, progress=progress)

    stored = sum(1 for r in results if r.ok)
    ctx.report.summary(**{
        "stored to fundamentals": f"{stored}/{len(results)}",
        "cost": "$0.00",
    })
    if stored == 0:
        # Two findings wear this same summary, and only one of them is an error.
        # Every filing read and refused is this company's answer — a bank tags
        # no revenue line and never will — so the job carries on to scoring,
        # which is the whole point of the run. A filing never read is a verdict
        # on the network, and continuing would report the free path as having
        # considered something it never opened.
        if all(r.refused for r in results):
            raise StepSkipped(
                "Every tagged filing was read and none carries the figures the "
                "model needs — see the warnings above. Banks are the usual "
                "case: their results taxonomy tags no revenue line. Those "
                "company-years stay uncovered."
            )
        raise RuntimeError(
            "No filing yielded a usable annual row, and at least one was not "
            "read at all — see the warnings above. That is a fetch failure "
            "rather than a verdict on the filings, so it is worth re-running "
            "before paying to read the annual reports."
        )
    uncovered = len(results) - stored
    if uncovered:
        ctx.report.warn(
            f"{uncovered} company-year(s) carry no usable tagged figures and "
            f"stay uncovered — see the warnings above for which, and why."
        )


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


def _publish(ctx: StepContext) -> None:
    """Copy the site's tables to Postgres and switch the API over atomically.

    Last on purpose. It publishes whatever the steps before it left in DuckDB,
    and a failed step ends the job before reaching it, so a broken run never
    reaches the site. Always the full set of site tables, even on a one-company
    run: a publish is a rebuild rather than a patch, which is what makes it
    safe to repeat. A cancel mid-copy abandons the release with nothing
    switched — see `db.publish`.
    """
    from stockanalysis.db.postgres import PostgresUnavailable
    from stockanalysis.db.publish import publish

    def progress(i: int, n: int, table: str, rows: int) -> None:
        ctx.report.row(table=table, rows=rows)
        ctx.report.progress(i, n, f"{i}/{n} tables")
        ctx.report.check_cancelled()

    try:
        result = publish(ctx.db, progress=progress)
    except PostgresUnavailable as e:
        raise StepSkipped(f"{e} The API keeps serving DuckDB.") from e

    ctx.report.summary(**{
        "release": result.release_id,
        "rows published": f"{result.total_rows:,}",
        "replaced": result.previous or "nothing — first publish",
    })
    ctx.report.log(
        f"Schema {result.schema!r} now serves release {result.release_id} "
        f"({len(result.row_counts)} tables, {result.seconds:.1f}s)"
    )
    for name in result.drop_failures:
        ctx.report.warn(
            f"Could not drop the old release {name}; the next publish retries it."
        )


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


def _xbrl_link_count(db: Database, isins: list[str] | None) -> int:
    """Company-years with a tagged annual instance — what `xbrl` can read.

    From `results_filings`, which is what `pending_annual_filings` reads.
    `fundamentals_quarterly` holds at most one row per quarter and only for the
    window the quarterly step covered, so counting there under-reported.
    """
    where, params = ("AND isin = ?", [isins[0]]) if isins else ("", [])
    df = db.query(
        f"SELECT COUNT(DISTINCT (isin, period_end_date)) AS c FROM results_filings "
        f"WHERE relating_to = 'Fourth Quarter' AND xbrl_url LIKE '%.xml' {where}",
        params,
    )
    return int(df["c"].iloc[0]) if not df.empty else 0


def _filing_count(db: Database, isins: list[str] | None, since: dt.date) -> int:
    """Results filings with an instance on file for the scope and window."""
    where, params = ("AND isin = ?", [isins[0]]) if isins else ("", [])
    df = db.query(
        f"SELECT COUNT(*) AS c FROM results_filings "
        f"WHERE xbrl_url LIKE '%.xml' AND period_end_date >= ? {where}",
        [since, *params],
    )
    return int(df["c"].iloc[0]) if not df.empty else 0


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
        key="results-index",
        label="Fetch NSE filing index",
        description=(
            "Every results filing on NSE — the legacy index and, since April "
            "2025, the integrated filing — with broadcast dates and the XBRL "
            "links the quarterly and annual steps read. Exchange-wide crawl."
        ),
        run=_results_index,
        cost=NETWORK,
    ),
    StepSpec(
        key="quarterly",
        label="Ingest quarterly results",
        description=(
            "Revenue, PAT and EPS per quarter from NSE's tagged results "
            "filings. Free, and needs no model."
        ),
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
        description=(
            "Fetch annual-report PDFs from NSE and register them. Off by "
            "default: nothing reads the PDFs since the model-backed extraction "
            "was retired, so a run downloads hundreds of MB that only `xbrl` "
            "would then duplicate for free. Kept runnable because the PDFs are "
            "the only route to a year the tagged filings do not cover."
        ),
        run=_filings,
        cost=NETWORK,
        default_on=False,
    ),
    StepSpec(
        key="xbrl",
        label="Read annual XBRL",
        description=(
            "Annual financials from NSE's tagged results filing — balance "
            "sheet and cash flow included. Free, and needs no model."
        ),
        run=_xbrl,
        cost=NETWORK,
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
    StepSpec(
        key="publish",
        label="Publish to Postgres",
        description=(
            "Copy the tables the API reads into Postgres and switch the live "
            "schema over in one transaction. Skips when SA_PG_DSN is unset."
        ),
        run=_publish,
        cost=FREE,
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
