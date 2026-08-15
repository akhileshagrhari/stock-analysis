"""Command-line interface."""

from __future__ import annotations

import datetime as dt
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from stockanalysis.backtest.benchmark import equal_weight_benchmark, format_comparison
from stockanalysis.backtest.engine import BacktestConfig, BacktestEngine, rebalance_dates
from stockanalysis.backtest.metrics import format_metrics
from stockanalysis.config import settings
from stockanalysis.db.database import Database, DatabaseLockedError, SchemaOutOfDateError
from stockanalysis.factors.momentum import Momentum12_1
from stockanalysis.ingest.prices import ingest_prices
from stockanalysis.universe.loader import backfill_membership_start, seed_index_from_nse

app = typer.Typer(help="Factor-based equity research for Indian markets", no_args_is_help=True)
console = Console()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
)


def open_db(read_only: bool = False) -> Database:
    """Open the database, turning a lock clash into a readable message."""
    try:
        return Database(settings.db_path, read_only=read_only)
    except DatabaseLockedError as e:
        console.print(f"[red]Database busy.[/red] {e}")
        raise typer.Exit(1) from e
    except SchemaOutOfDateError as e:
        console.print(f"[red]Schema out of date.[/red] {e}")
        raise typer.Exit(1) from e
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@contextmanager
def extractor_errors():
    """Turn an unreachable extraction backend into a readable message.

    Wraps the call, not just the construction: the bake-off builds its
    extractors one model at a time inside `run_bakeoff`, so the credential
    check does not surface until it is already several frames down.
    """
    from stockanalysis.extract.factory import ExtractorUnavailableError

    try:
        yield
    except ExtractorUnavailableError as e:
        console.print(f"[red]Extraction backend unavailable.[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def init() -> None:
    """Create data directories and initialise the database."""
    settings.ensure_dirs()
    with open_db() as db:
        tables = db.query("SELECT table_name FROM information_schema.tables ORDER BY 1")
    console.print(f"[green]Initialised[/green] {settings.db_path}")
    console.print(f"  {len(tables)} tables created")


@app.command("seed-universe")
def seed_universe(
    index_name: str = typer.Option(settings.default_index, "--index"),
    backfill_from: str = typer.Option(
        None,
        "--backfill-from",
        help="Push members' from_date back to this date (YYYY-MM-DD). "
             "A MODELLING ASSUMPTION that makes backtests runnable but keeps "
             "them survivorship-unsafe.",
    ),
) -> None:
    """Fetch index constituents from NSE and seed instruments + membership."""
    settings.ensure_dirs()
    with open_db() as db:
        n = seed_index_from_nse(db, index_name)
        console.print(f"[green]Seeded[/green] {n} instruments for {index_name}")

        if backfill_from:
            d = dt.date.fromisoformat(backfill_from)
            backfill_membership_start(db, index_name, d)
            console.print(
                f"[yellow]Backfilled[/yellow] membership start to {d} "
                f"(assumption, not data — universe remains survivorship-unsafe)"
            )


@app.command("ingest-prices")
def ingest_prices_cmd(
    years: int = typer.Option(6, "--years", help="How far back to fetch"),
    limit: int = typer.Option(None, "--limit", help="Only first N instruments (testing)"),
) -> None:
    """Fetch and store daily prices for all seeded instruments."""
    settings.ensure_dirs()
    end = dt.date.today()
    start = end - dt.timedelta(days=365 * years)

    with open_db() as db:
        isins = db.query("SELECT isin FROM instruments ORDER BY isin")["isin"].tolist()
        if limit:
            isins = isins[:limit]
        if not isins:
            console.print("[red]No instruments.[/red] Run `seed-universe` first.")
            raise typer.Exit(1)

        console.print(f"Ingesting {len(isins)} instruments from {start} to {end}...")
        n = ingest_prices(db, isins=isins, start=start, end=end)
        console.print(f"[green]Stored[/green] {n:,} price rows")


def _build_factor(name: str, min_coverage: float, no_red_flags: bool):
    """Resolve a --factor name to a Factor instance."""
    from stockanalysis.factors.composite import (
        CompositeModel,
        ScoringConfig,
        default_factors,
    )

    if name == "momentum":
        return Momentum12_1()
    if name == "composite":
        return CompositeModel(
            config=ScoringConfig(
                min_coverage=min_coverage, apply_red_flags=not no_red_flags
            )
        )

    single = {f.name: f for f in default_factors()}
    if name in single:
        return single[name]

    console.print(
        f"[red]Unknown factor '{name}'.[/red] Choose composite, momentum, or one of:\n"
        f"  {', '.join(sorted(single))}"
    )
    raise typer.Exit(1)


@app.command()
def backtest(
    index_name: str = typer.Option(settings.default_index, "--index"),
    start: str = typer.Option("2020-01-01", "--start"),
    end: str = typer.Option(None, "--end"),
    top_n: int = typer.Option(settings.top_n, "--top-n"),
    factor: str = typer.Option(
        "momentum", "--factor", help="composite, momentum, or a single factor name"
    ),
    min_coverage: float = typer.Option(
        0.5,
        "--min-coverage",
        help="Composite only: fraction of model weight that must be backed by "
             "data before a company is scored. Lower it deliberately to run on "
             "partial data.",
    ),
    no_red_flags: bool = typer.Option(
        False, "--no-red-flags", help="Composite only: disable the §6.2 overlay"
    ),
    no_costs: bool = typer.Option(False, "--no-costs", help="Disable transaction costs"),
) -> None:
    """Run the walk-forward backtest."""
    cfg = BacktestConfig(
        index_name=index_name,
        start=dt.date.fromisoformat(start),
        end=dt.date.fromisoformat(end) if end else dt.date.today(),
        top_n=top_n,
        apply_costs=not no_costs,
    )
    model = _build_factor(factor, min_coverage, no_red_flags)
    label = model.name

    with open_db() as db:
        result = BacktestEngine(db, model, cfg).run()
        # Without this, the headline CAGR is uninterpretable — it says nothing
        # about whether the factor added anything over holding the universe.
        _, bench_metrics = equal_weight_benchmark(
            db, index_name, rebalance_dates(cfg.start, cfg.end, cfg.rebalance_freq)
        )

    for w in result.warnings:
        console.print(f"[yellow]WARNING[/yellow]  {w}\n")

    console.print(format_metrics(result.metrics, f"{label} | {index_name}"))
    console.print(format_comparison(result.metrics, bench_metrics, label))
    console.print(f"  run_id: {result.run_id}")

    if result.metrics.sharpe > 3.0:
        console.print(
            "\n[red bold]Implausible Sharpe.[/red bold] Cross-sectional equity "
            "factors do not produce this. Suspect lookahead bias before believing it."
        )


@app.command()
def score(
    index_name: str = typer.Option(settings.default_index, "--index"),
    as_of: str = typer.Option(None, "--as-of", help="Decision date (default: today)"),
    top: int = typer.Option(15, "--top", help="How many names to show each way"),
    min_coverage: float = typer.Option(0.5, "--min-coverage"),
    no_red_flags: bool = typer.Option(False, "--no-red-flags"),
    persist_scores: bool = typer.Option(
        False, "--persist", help="Write to factor_scores and signals"
    ),
) -> None:
    """Score the universe as it stood on a date — DESIGN §6's composite."""
    from stockanalysis.factors.composite import ScoringConfig, persist, score_as_of
    from stockanalysis.factors.redflags import unreachable_flags

    date = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    cfg = ScoringConfig(min_coverage=min_coverage, apply_red_flags=not no_red_flags)

    with open_db(read_only=not persist_scores) as db:
        result = score_as_of(db, index_name, date, cfg)
        if persist_scores:
            n_f, n_s = persist(db, result)
            console.print(f"[green]Persisted[/green] {n_f} factor scores, {n_s} signals")

    table_df = result.table()
    scored = table_df[table_df["signal"].notna()]

    console.print(f"\n[bold]{index_name} as of {date}[/bold]  ({result.version})")
    console.print(
        f"  universe {len(table_df)}   scored {len(scored)}   "
        f"median coverage {result.coverage.median():.0%}"
    )

    # Coverage per family, because "median coverage 15%" does not say *which*
    # 85% is missing, and that is the actionable part.
    fam_cov = _family_coverage(result)
    console.print("  families with data: " + ", ".join(
        f"{k} {v:.0%}" for k, v in fam_cov.items()
    ))

    unreachable = unreachable_flags()
    if unreachable:
        console.print(
            f"  [yellow]not evaluable:[/yellow] {', '.join(unreachable)} — "
            f"no ingested source supplies them, so a clean run does not clear them"
        )

    if scored.empty:
        console.print(
            f"\n[yellow]Nothing scored.[/yellow] No company met --min-coverage "
            f"{min_coverage:.0%}. The fundamental families need the phase-1 "
            f"backfill; until then run with a lower threshold and read the result "
            f"as the factors that were actually available."
        )
        return

    counts = scored["signal"].value_counts()
    console.print("  " + "   ".join(f"{k} {v}" for k, v in counts.items()) + "\n")

    ranked = scored.sort_values("score", ascending=False)
    _print_scores(ranked.head(top), f"Top {min(top, len(ranked))}")
    _print_scores(ranked.tail(top).iloc[::-1], f"Bottom {min(top, len(ranked))}")


def _family_coverage(result) -> dict[str, float]:
    """Fraction of the universe with a computable score, per family."""
    return {
        fam: float(result.family_z[fam].notna().mean())
        for fam in result.family_z.columns
    }


def _print_scores(df, title: str) -> None:
    table = Table(title=title)
    table.add_column("ISIN")
    table.add_column("Score", justify="right")
    table.add_column("Signal")
    table.add_column("Cov", justify="right")
    table.add_column("Red flags")
    for isin, r in df.iterrows():
        colour = {"BUY": "green", "HOLD": "yellow", "SELL": "red"}.get(r["signal"], "")
        table.add_row(
            isin,
            f"{r['score']:.1f}",
            f"[{colour}]{r['signal']}[/{colour}]" if colour else str(r["signal"]),
            f"{r['coverage']:.0%}",
            r["red_flags"] or "-",
        )
    console.print(table)


@app.command()
def attribution(
    index_name: str = typer.Option(settings.default_index, "--index"),
    start: str = typer.Option("2021-01-01", "--start"),
    end: str = typer.Option(None, "--end"),
) -> None:
    """Per-factor information coefficient and decile spread.

    The backtest reports whether a portfolio worked. This reports which factors
    did, which is the only way to tell a working model from one carried by a
    single factor while the rest add noise.
    """
    from stockanalysis.backtest.attribution import format_attribution, run_attribution
    from stockanalysis.factors.composite import default_factors

    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end) if end else dt.date.today()
    # Same month-end grid the backtest rebalances on, so the two are measuring
    # the same decisions.
    dates = rebalance_dates(start_d, end_d, BacktestConfig.rebalance_freq)

    with open_db(read_only=True) as db:
        df = run_attribution(db, index_name, dates, default_factors())

    console.print(format_attribution(df, f"Factor attribution | {index_name} "
                                         f"{start_d}..{end_d}"))

    uncomputable = df[df["periods"] == 0]["factor"].tolist()
    if uncomputable:
        console.print(
            f"  [yellow]{len(uncomputable)} factors had no data on any date:[/yellow] "
            f"{', '.join(uncomputable)}\n"
            f"  These need the phase-1 annual-report backfill (or phase-3 news).\n"
        )


# ======================================================================
# Phase 1b — free NSE fundamentals (no API key required)
# ======================================================================


@app.command("ingest-shareholding")
def ingest_shareholding_cmd(
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Fetch quarterly shareholding patterns. Free, no LLM.

    Supports the 'promoter holding falling three consecutive quarters' red flag.
    Does NOT supply promoter pledge — that is a separate disclosure NSE does not
    expose here, so `promoter_pledged_pct` stays NULL and must be read as
    unknown, never as zero.
    """
    from stockanalysis.ingest.shareholding import ingest_shareholding

    settings.ensure_dirs()
    with open_db() as db:
        isins = db.query("SELECT isin FROM instruments ORDER BY isin")["isin"].tolist()
        if limit:
            isins = isins[:limit]
        n = ingest_shareholding(db, isins=isins)
        console.print(f"[green]Stored[/green] {n} shareholding rows")


@app.command("ingest-results-index")
def ingest_results_index_cmd(
    years: int = typer.Option(3, "--years"),
) -> None:
    """Replace assumed quarterly knowledge dates with NSE's real broadcast dates.

    One request per 90-day window rather than one per company — the filing index
    is exchange-wide, so this costs a handful of calls instead of hundreds.
    """
    from stockanalysis.ingest.nse_fundamentals import ingest_results_index

    settings.ensure_dirs()
    end = dt.date.today()
    with open_db() as db:
        before = db.query(
            "SELECT COUNT(*) AS c FROM fundamentals_quarterly "
            "WHERE filing_date_source = 'NSE'"
        )["c"].iloc[0]
        n = ingest_results_index(db, from_date=end - dt.timedelta(days=365 * years),
                                 to_date=end)
        after = db.query(
            "SELECT COUNT(*) AS c FROM fundamentals_quarterly "
            "WHERE filing_date_source = 'NSE'"
        )["c"].iloc[0]

    console.print(f"[green]Applied[/green] {n} filing-index matches")
    console.print(f"  quarterly rows with a real broadcast date: {before} -> {after}")


@app.command("local-models")
def local_models_cmd(
    base_url: str = typer.Option(None, "--base-url"),
) -> None:
    """List models loaded in LM Studio, for use as --model local:<id>."""
    from stockanalysis.extract.local import DEFAULT_BASE_URL, list_local_models

    url = base_url or DEFAULT_BASE_URL
    try:
        models = list_local_models(url)
    except Exception as e:  # noqa: BLE001 - surface any connection problem plainly
        console.print(f"[red]Cannot reach LM Studio at {url}[/red]: {e}")
        console.print("Start it with:  lms server start")
        raise typer.Exit(1) from e

    if not models:
        console.print("[yellow]Server is up but no model is loaded.[/yellow]")
        return
    for m in models:
        console.print(f"  local:{m}")
    console.print(
        "\n[dim]Local extraction sends flattened text, not the PDF, and truncates "
        "to fit context. Expect it to miss DESIGN's 95% bar — run `bakeoff` to "
        "find out by how much.[/dim]"
    )


# ======================================================================
# Phase 1 — extraction
# ======================================================================


def _batch_manifest(batch_id: str) -> Path:
    """Where a submitted batch's filing list is remembered between commands.

    Submit and collect are separate invocations, potentially hours apart. The
    filing set has to survive that gap so results can be paired back to their
    filings by custom_id.
    """
    d = settings.data_dir / "batches"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{batch_id}.json"


@app.command("fetch-filings")
def fetch_filings_cmd(
    years: int = typer.Option(settings.filing_years, "--years"),
    limit: int = typer.Option(None, "--limit", help="Only first N companies"),
) -> None:
    """Download annual-report PDFs from NSE and register them in `filings`."""
    from stockanalysis.ingest.filings import fetch_annual_reports

    settings.ensure_dirs()
    with open_db() as db:
        isins = db.query("SELECT isin FROM instruments ORDER BY isin")["isin"].tolist()
        if limit:
            isins = isins[:limit]
        if not isins:
            console.print("[red]No instruments.[/red] Run `seed-universe` first.")
            raise typer.Exit(1)

        console.print(
            f"Fetching up to {years} years of annual reports for {len(isins)} "
            f"companies (~{settings.request_delay_seconds}s between requests)..."
        )
        n = fetch_annual_reports(db, isins=isins, years=years)
        console.print(f"[green]Registered[/green] {n} filings")

        assumed = db.query(
            "SELECT COUNT(*) AS c FROM filings "
            "WHERE broadcast_date_source = 'ASSUMED_AGM_DEADLINE'"
        )["c"].iloc[0]
        if assumed:
            console.print(
                f"[yellow]{assumed} filings[/yellow] have an assumed knowledge date "
                f"(period end + 6 months, the statutory AGM deadline). NSE did not "
                f"supply a broadcast date. This is deliberately late, so backtests "
                f"understate rather than fabricate signal."
            )


@app.command("ingest-quarterly")
def ingest_quarterly_cmd(
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Fetch NSE quarterly results — the cross-check for LLM extraction."""
    from stockanalysis.ingest.nse_fundamentals import ingest_quarterly

    settings.ensure_dirs()
    with open_db() as db:
        isins = db.query("SELECT isin FROM instruments ORDER BY isin")["isin"].tolist()
        if limit:
            isins = isins[:limit]
        n = ingest_quarterly(db, isins=isins)
        console.print(f"[green]Stored[/green] {n} quarterly rows")


@app.command()
def extract(
    limit: int = typer.Option(10, "--limit", help="How many filings to extract"),
    isin: str = typer.Option(None, "--isin", help="Comma-separated ISINs to restrict to"),
    fiscal_year: str = typer.Option(None, "--fy", help="Comma-separated fiscal years"),
    model: str = typer.Option(settings.extraction_model, "--model"),
    label: str = typer.Option("adhoc", "--label", help="Tag for extraction_attempts"),
    redo: bool = typer.Option(False, "--redo", help="Re-extract already-done filings"),
) -> None:
    """Extract financials from downloaded annual reports (synchronous)."""
    from stockanalysis.extract.factory import make_extractor
    from stockanalysis.extract.pipeline import pending_filings, run_extraction

    isins = [s.strip() for s in isin.split(",") if s.strip()] if isin else None
    years = [int(s) for s in fiscal_year.split(",") if s.strip()] if fiscal_year else None

    with open_db() as db:
        filings = pending_filings(
            db,
            limit=limit,
            isins=isins,
            fiscal_years=years,
            only_unextracted=not redo,
            model=model,
        )
        if not filings:
            console.print("[yellow]Nothing to extract.[/yellow] Run `fetch-filings` first.")
            raise typer.Exit(0)

        console.print(f"Extracting {len(filings)} filings with {model}...")
        with extractor_errors():
            extractor = make_extractor(model)

        def show(i, n, filing, result, report):
            if result.error:
                console.print(f"  [{i}/{n}] {filing.symbol} FY{filing.fiscal_year} "
                              f"[red]{result.error}[/red]")
            else:
                c = report.confidence if report else 0.0
                colour = "green" if c >= 1.0 else "yellow" if c >= 0.6 else "red"
                console.print(
                    f"  [{i}/{n}] {filing.symbol} FY{filing.fiscal_year} "
                    f"[{colour}]confidence {c}[/{colour}] "
                    f"${result.cost_usd():.3f} {result.latency_seconds:.0f}s"
                )

        results = run_extraction(db, filings, extractor, run_label=label, progress=show)

    ok = sum(1 for r, _ in results if r.ok)
    cost = sum(r.cost_usd() for r, _ in results)
    console.print(f"\n[green]{ok}/{len(results)}[/green] extracted, ${cost:.2f} spent")


@app.command("extract-batch")
def extract_batch_cmd(
    limit: int = typer.Option(100, "--limit"),
    model: str = typer.Option(settings.extraction_model, "--model"),
    redo: bool = typer.Option(False, "--redo"),
) -> None:
    """Submit a backfill batch. 50% cheaper; results in under 24h."""
    from stockanalysis.extract.claude import ClaudeExtractor
    from stockanalysis.extract.pipeline import pending_filings, submit_batch

    with open_db() as db:
        filings = pending_filings(db, limit=limit, only_unextracted=not redo, model=model)
        if not filings:
            console.print("[yellow]Nothing to extract.[/yellow]")
            raise typer.Exit(0)

        console.print(f"Locating sections in {len(filings)} filings...")
        with extractor_errors():
            batch_id, jobs, skipped = submit_batch(db, filings, ClaudeExtractor(model=model))

        _batch_manifest(batch_id).write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "model": model,
                    "submitted_at": dt.datetime.now().isoformat(),
                    "filing_ids": [f.filing_id for f in filings],
                },
                indent=2,
            )
        )

    console.print(f"[green]Submitted[/green] batch {batch_id} ({len(jobs)} requests)")
    for filing, reason in skipped:
        console.print(f"  [yellow]skipped[/yellow] {filing.filing_id}: {reason}")
    console.print(f"\nPoll with:  stockanalysis batch-status {batch_id}")


@app.command("batch-status")
def batch_status_cmd(batch_id: str) -> None:
    """Check a submitted extraction batch."""
    from stockanalysis.extract.claude import ClaudeExtractor

    manifest = _batch_manifest(batch_id)
    model = json.loads(manifest.read_text())["model"] if manifest.exists() else None
    with extractor_errors():
        status_, counts = ClaudeExtractor(model=model).batch_status(batch_id)
    console.print(f"{batch_id}: [bold]{status_}[/bold]")
    for k, v in counts.items():
        console.print(f"  {k}: {v}")
    if status_ == "ended":
        console.print(f"\nCollect with:  stockanalysis batch-collect {batch_id}")


@app.command("batch-collect")
def batch_collect_cmd(
    batch_id: str,
    label: str = typer.Option("backfill", "--label"),
) -> None:
    """Fetch, validate and persist a completed extraction batch."""
    from stockanalysis.extract.claude import ClaudeExtractor
    from stockanalysis.extract.pipeline import collect_batch, pending_filings

    manifest = _batch_manifest(batch_id)
    if not manifest.exists():
        console.print(f"[red]No manifest for {batch_id}.[/red] It records which "
                      f"filings were submitted; without it results cannot be paired.")
        raise typer.Exit(1)

    meta = json.loads(manifest.read_text())
    with open_db() as db:
        filings = pending_filings(db, only_unextracted=False)
        wanted = set(meta["filing_ids"])
        filings = [f for f in filings if f.filing_id in wanted]

        console.print(f"Collecting {len(filings)} results from {batch_id}...")
        with extractor_errors():
            results = collect_batch(
                db, batch_id, filings, ClaudeExtractor(model=meta["model"]), run_label=label
            )

    ok = sum(1 for r, _ in results if r.ok)
    clean = sum(1 for _, rep in results if rep and rep.confidence >= 1.0)
    cost = sum(r.cost_usd() for r, _ in results)
    console.print(
        f"[green]{ok}/{len(results)}[/green] extracted, "
        f"{clean} passed every validator, ${cost:.2f} spent"
    )


@app.command()
def bakeoff(
    n: int = typer.Option(10, "--n", help="Filings to test (DESIGN suggests 10)"),
    models: str = typer.Option(
        "claude-opus-5,claude-sonnet-5", "--models", help="Comma-separated"
    ),
) -> None:
    """Compare extraction models on the same filings. Settles the model choice."""
    from stockanalysis.extract.bakeoff import format_bakeoff, run_bakeoff
    from stockanalysis.extract.pipeline import pending_filings

    model_list = [m.strip() for m in models.split(",") if m.strip()]
    with open_db() as db:
        filings = pending_filings(db, limit=n, only_unextracted=False)
        if not filings:
            console.print("[red]No filings.[/red] Run `fetch-filings` first.")
            raise typer.Exit(1)

        quarterly = db.query("SELECT COUNT(*) AS c FROM fundamentals_quarterly")["c"].iloc[0]
        if not quarterly:
            console.print(
                "[yellow]No quarterly data.[/yellow] The NSE cross-check is the "
                "only score using evidence from outside the PDF — run "
                "`ingest-quarterly` first or the bake-off compares models only "
                "on self-consistency."
            )

        console.print(f"Running {len(filings)} filings x {len(model_list)} models...")
        with extractor_errors():
            result = run_bakeoff(db, filings, model_list)

    format_bakeoff(result, console)


@app.command()
def review(limit: int = typer.Option(25, "--limit")) -> None:
    """Show extractions awaiting human review."""
    from stockanalysis.extract.review import pending, summary

    with open_db(read_only=True) as db:
        counts = summary(db)
        if not counts.empty:
            console.print(counts.to_string(index=False))
            console.print()

        df = pending(db, limit)
        if df.empty:
            console.print("[green]Review queue empty.[/green]")
            return

        table = Table(title="Pending extraction review")
        table.add_column("attempt_id")
        table.add_column("Symbol")
        table.add_column("FY", justify="right")
        table.add_column("Conf", justify="right")
        table.add_column("Why")
        for r in df.itertuples(index=False):
            table.add_row(
                r.attempt_id,
                r.nse_symbol or r.isin,
                str(r.fiscal_year),
                f"{r.confidence:.1f}",
                (r.reasons or "")[:90],
            )
        console.print(table)
        console.print("\n[dim]Inspect: stockanalysis review-detail <attempt_id>[/dim]")


@app.command("review-detail")
def review_detail_cmd(attempt_id: str) -> None:
    """Full extraction, validator results and source PDF for one attempt."""
    from stockanalysis.extract.review import detail

    with open_db(read_only=True) as db:
        row = detail(db, attempt_id)

    console.print(f"[bold]{row.get('name')}[/bold] ({row.get('nse_symbol')}) "
                  f"FY{row['fiscal_year']}  model={row['model']}")
    console.print(f"  PDF:    {row['local_path']}")
    console.print(f"  Pages:  {row['source_pages']} ({row['pages_sent']} sent)")
    console.print(f"  Known:  {row['broadcast_date']} [{row['broadcast_date_source']}]")
    console.print(f"  Cost:   ${row['cost_usd']:.3f}\n")

    if row["checks"]:
        table = Table(title=f"Validators — confidence {row['checks']['confidence']}")
        table.add_column("Check")
        table.add_column("Result")
        table.add_column("Detail")
        for c in row["checks"]["checks"]:
            state = (
                "[dim]skipped[/dim]" if c["skipped"]
                else "[green]pass[/green]" if c["passed"]
                else f"[red]FAIL ({c['severity']})[/red]"
            )
            table.add_row(c["name"], state, c["detail"])
        console.print(table)

    if row["payload"]:
        console.print("\n[bold]Extraction[/bold] (as reported, before unit conversion)")
        for k, v in row["payload"].items():
            if v is not None:
                console.print(f"  {k}: {v}")


@app.command("review-resolve")
def review_resolve_cmd(
    attempt_id: str,
    accept: bool = typer.Option(False, "--accept"),
    reject: bool = typer.Option(False, "--reject"),
    notes: str = typer.Option(None, "--notes"),
    persist: bool = typer.Option(
        False, "--persist", help="On accept, write to fundamentals even below threshold"
    ),
) -> None:
    """Accept or reject a queued extraction."""
    from stockanalysis.extract.review import resolve

    if accept == reject:
        console.print("[red]Pass exactly one of --accept or --reject.[/red]")
        raise typer.Exit(1)

    with open_db() as db:
        resolve(
            db,
            attempt_id,
            "ACCEPTED" if accept else "REJECTED",
            notes=notes,
            force_persist=persist,
        )
    console.print(f"[green]{'Accepted' if accept else 'Rejected'}[/green] {attempt_id}")


# ======================================================================
# Phase 3 — news and sentiment
# ======================================================================


@app.command("build-aliases")
def build_aliases_cmd() -> None:
    """Rebuild the headline-text -> ISIN alias table.

    Prerequisite for every news command. Prints the aliases it refused to
    assign, because a name two listed companies share is a coverage hole with a
    cause, not a bug.
    """
    from stockanalysis.news.aliases import build_aliases

    with open_db() as db:
        n, conflicts = build_aliases(db)
        by_source = db.query(
            "SELECT source, COUNT(*) AS n FROM instrument_aliases GROUP BY 1 ORDER BY 2 DESC"
        )

    console.print(f"[green]Built[/green] {n} aliases")
    for r in by_source.itertuples(index=False):
        console.print(f"  {r.source:<12} {r.n}")

    if conflicts:
        console.print(
            f"\n[yellow]{len(conflicts)} aliases dropped[/yellow] — claimed by "
            f"more than one instrument, so neither gets them:"
        )
        for alias, isins in conflicts[:15]:
            console.print(f"  {alias!r} -> {', '.join(isins)}")


@app.command("ingest-news")
def ingest_news_cmd(
    since_days: int = typer.Option(
        None, "--since-days", help="Ignore items older than this"
    ),
) -> None:
    """Fetch the configured RSS feeds. Free, no key, no history.

    RSS returns only the current front page of each feed, so this builds an
    archive going forward and cannot fill one in. `backfill-news` is the
    historical path.
    """
    from stockanalysis.ingest.rss import ingest_rss
    from stockanalysis.news.resolve import EmptyAliasTableError

    settings.ensure_dirs()
    since = (
        dt.datetime.now() - dt.timedelta(days=since_days) if since_days else None
    )
    with open_db() as db:
        try:
            stats, per_feed = ingest_rss(db, since=since)
        except EmptyAliasTableError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

    for url, n in per_feed.items():
        name = url.split("/")[2]
        state = "[red]failed[/red]" if n < 0 else f"{n} items"
        console.print(f"  {name:<32} {state}")

    console.print(
        f"\n[green]Stored[/green] {stats.stored} rows from {stats.fetched} articles"
    )
    console.print(
        f"  resolved {stats.resolved}   unresolved {stats.unresolved}   "
        f"duplicates {stats.duplicates}   below threshold {stats.below_threshold}"
    )
    if stats.by_method:
        console.print("  by method: " + ", ".join(
            f"{k} {v}" for k, v in sorted(stats.by_method.items())
        ))
    console.print(
        f"  resolution rate {stats.resolution_rate:.0%} — the rest are index "
        f"and macro stories that name no company in the universe"
    )


@app.command("reresolve-news")
def reresolve_news_cmd() -> None:
    """Re-run ticker resolution over already-stored articles.

    Run this after `build-aliases`. Improving the alias table otherwise only
    affects future fetches, and for GDELT the next fetch is hours of
    rate-limited requests — which in practice means the fix never gets made.
    """
    from stockanalysis.news.resolve import TickerResolver
    from stockanalysis.news.store import reresolve

    with open_db() as db:
        stats = reresolve(
            db, TickerResolver.from_db(db), settings.news_min_resolution_confidence
        )

    console.print(
        f"[green]Re-resolved[/green] {stats.articles} articles, "
        f"{stats.changed} changed"
    )
    console.print(
        f"  newly resolved {stats.newly_resolved}   lost {stats.lost}   "
        f"duplicate stories removed {stats.duplicates_removed}   "
        f"rows {stats.rows_before} -> {stats.rows_after}"
    )
    if stats.lost:
        console.print(
            f"  [yellow]{stats.lost}[/yellow] articles lost their company — "
            f"their sentiment scores went with them, which is correct if the "
            f"attribution was wrong and a loss of coverage if it was not"
        )


@app.command("ingest-marketaux")
def ingest_marketaux_cmd(
    limit: int = typer.Option(None, "--limit", help="Only first N instruments"),
    max_requests: int = typer.Option(20, "--max-requests", help="Free tier is 100/day"),
) -> None:
    """Fetch entity-tagged news from Marketaux. Needs SA_MARKETAUX_API_KEY."""
    from stockanalysis.ingest.marketaux import (
        MarketauxUnavailableError,
        ingest_marketaux,
    )

    settings.ensure_dirs()
    with open_db() as db:
        isins = db.query("SELECT isin FROM instruments ORDER BY isin")["isin"].tolist()
        if limit:
            isins = isins[:limit]
        try:
            stats = ingest_marketaux(db, isins=isins, max_requests=max_requests)
        except MarketauxUnavailableError as e:
            console.print(f"[yellow]Skipped.[/yellow] {e}")
            raise typer.Exit(0) from e

    console.print(f"[green]Stored[/green] {stats.stored} rows ({stats})")


@app.command("backfill-news")
def backfill_news_cmd(
    start: str = typer.Option("2021-01-01", "--start"),
    end: str = typer.Option(None, "--end"),
    limit: int = typer.Option(None, "--limit", help="Only first N companies"),
    max_requests: int = typer.Option(
        200, "--max-requests", help="Stop after this many GDELT calls"
    ),
) -> None:
    """Backfill historical news from GDELT so the sentiment factor is testable.

    One request per company per month at one request every six seconds — a
    Nifty 100 x 3-year backfill is ~3,600 requests and about six hours. Every
    window is checkpointed, so this is safe to interrupt and re-run; it resumes
    rather than restarting.
    """
    from stockanalysis.ingest.gdelt import backfill_gdelt, pending_windows
    from stockanalysis.news.resolve import EmptyAliasTableError

    settings.ensure_dirs()
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end) if end else dt.date.today()

    with open_db() as db:
        isins = db.query("SELECT isin FROM instruments ORDER BY isin")["isin"].tolist()
        if limit:
            isins = isins[:limit]

        try:
            outstanding = pending_windows(db, isins, start_d, end_d)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

        # Paced at the documented rate this would be one request per
        # `gdelt_delay_seconds`. Measured, it is 6-12 completed windows an
        # hour, because most requests come back 429 and are retried. Quoting
        # the pacing figure would understate the job by an order of magnitude,
        # so the range is the measured one.
        planned = min(len(outstanding), max_requests)
        console.print(
            f"{len(outstanding)} windows outstanding; running {planned} "
            f"(~{planned / 12:.0f}-{planned / 6:.0f} h at observed throughput)"
        )
        console.print(
            "  [dim]GDELT throttles well below its documented rate. Every "
            "window is checkpointed — interrupt and re-run to resume.[/dim]"
        )

        def show(i, n, w, stats):
            if i % 10 == 0 or i == n:
                console.print(
                    f"  [{i}/{n}] {w.isin} {w.start:%Y-%m}  "
                    f"+{stats.resolved} resolved, {stats.unconfirmed} unconfirmed"
                )

        try:
            stats, done = backfill_gdelt(
                db, isins, start_d, end_d,
                max_requests=max_requests, progress=show,
            )
        except EmptyAliasTableError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

        remaining = len(pending_windows(db, isins, start_d, end_d))

    console.print(f"\n[green]Completed[/green] {done} windows, {remaining} remaining")
    console.print(f"  {stats}")
    console.print(
        f"  [yellow]{stats.unconfirmed}[/yellow] articles dropped: GDELT's "
        f"full-text match did not name the company in the title"
    )


@app.command("score-news")
def score_news_cmd(
    limit: int = typer.Option(None, "--limit"),
    model: str = typer.Option(settings.sentiment_model, "--model"),
) -> None:
    """Run FinBERT over unscored news. Local, free, CPU."""
    from stockanalysis.news.finbert import FinBertScorer, ScorerUnavailableError
    from stockanalysis.news.scoring import pending_news, score_news

    with open_db() as db:
        outstanding = len(pending_news(db, model))
        if not outstanding:
            console.print("[green]Nothing to score.[/green]")
            return

        console.print(f"Loading {model}...")
        try:
            scorer = FinBertScorer(model_name=model)
        except ScorerUnavailableError as e:
            console.print(f"[red]Scorer unavailable.[/red] {e}")
            raise typer.Exit(1) from e

        console.print(f"Scoring {outstanding if not limit else min(limit, outstanding)} "
                      f"rows on {scorer.device}...")

        def show(done, total):
            console.print(f"  {done}/{total}")

        stats = score_news(db, scorer, limit=limit, progress=show)

    console.print(f"[green]Scored[/green] {stats.scored} rows")
    console.print(
        "  " + "  ".join(f"{k} {v}" for k, v in sorted(stats.by_label.items()))
    )
    if stats.reused:
        console.print(
            f"  {stats.reused} rows reused an identical text's score "
            f"(multi-company articles)"
        )


@app.command("news-status")
def news_status_cmd(
    model: str = typer.Option(settings.sentiment_model, "--model"),
) -> None:
    """News coverage and attribution — is the sentiment factor computable?"""
    from stockanalysis.news.scoring import coverage_report, resolution_report

    with open_db(read_only=True) as db:
        res = resolution_report(db)
        cov = coverage_report(db, model)
        universe = db.query("SELECT COUNT(*) AS c FROM instruments")["c"].iloc[0]

    if res.empty:
        console.print("[yellow]No news ingested.[/yellow] Run `ingest-news`.")
        return

    table = Table(title="Attribution")
    for col in ("Provider", "Method", "Rows", "Avg conf"):
        table.add_column(col, justify="right" if col in ("Rows", "Avg conf") else "left")
    for r in res.itertuples(index=False):
        table.add_row(r.provider or "-", r.method, f"{r.rows:,}",
                      "-" if pd.isna(r.avg_conf) else f"{r.avg_conf:.2f}")
    console.print(table)

    if not cov.empty:
        table = Table(title=f"Monthly coverage (universe {universe})")
        for col in ("Month", "Articles", "Companies", "Scored"):
            table.add_column(col, justify="right" if col != "Month" else "left")
        for r in cov.tail(24).itertuples(index=False):
            table.add_row(f"{r.month}", f"{r.articles:,}", f"{r.companies}",
                          f"{r.scored:,}")
        console.print(table)

        # The number that decides whether §6.1's 10% weight is real: the factor
        # needs MIN_ARTICLES in a 30-day window before it will produce a value.
        from stockanalysis.factors.sentiment import MIN_ARTICLES

        share = (cov["companies"] / universe).median()
        console.print(
            f"  median month covers [bold]{share:.0%}[/bold] of the universe; "
            f"the factor needs {MIN_ARTICLES}+ articles per company per 30 days"
        )


@app.command()
def status() -> None:
    """Show what data is currently loaded."""
    # Opened read-only to signal intent and avoid schema writes. Note this does
    # NOT allow concurrent access during an ingest: DuckDB takes a
    # process-exclusive lock on the database file, so a second process is
    # blocked even for reads. Commands are effectively serial; open_db turns
    # the resulting IOException into a readable message.
    with open_db(read_only=True) as db:
        table = Table(title="Data status")
        table.add_column("Table")
        table.add_column("Rows", justify="right")
        table.add_column("Range")

        for name, date_col in [
            ("instruments", None),
            ("index_membership", None),
            ("prices_daily", "date"),
            ("filings", "broadcast_date"),
            ("fundamentals_annual", "filing_date"),
            ("fundamentals_quarterly", "filing_date"),
            ("extraction_attempts", "created_at"),
            ("news", "published_at"),
            ("news_sentiment", "computed_at"),
            ("backtest_runs", None),
        ]:
            n = db.query(f"SELECT COUNT(*) AS c FROM {name}")["c"].iloc[0]
            rng = ""
            if date_col and n:
                r = db.query(f"SELECT MIN({date_col}) a, MAX({date_col}) b FROM {name}")
                rng = f"{r['a'].iloc[0]} to {r['b'].iloc[0]}"
            table.add_row(name, f"{n:,}", rng)

        console.print(table)

        idx = db.query("SELECT DISTINCT index_name FROM index_membership")
        for index_name in idx["index_name"]:
            safe = db.membership_is_survivorship_safe(
                index_name, dt.date(2015, 1, 1), dt.date.today()
            )
            flag = "[green]safe[/green]" if safe else "[yellow]UNSAFE (snapshot)[/yellow]"
            console.print(f"  {index_name} survivorship: {flag}")

        # Knowledge dates decide whether the fundamentals are backtestable at
        # all, so they belong next to the survivorship flag rather than buried
        # in a log line at ingest time.
        src = db.query(
            "SELECT broadcast_date_source AS s, COUNT(*) AS n FROM filings "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        for r in src.itertuples(index=False):
            colour = "green" if r.s == "NSE" else "yellow"
            console.print(f"  filing knowledge dates [{colour}]{r.s}[/{colour}]: {r.n}")

        pending_review = db.query(
            "SELECT COUNT(*) AS c FROM extraction_review WHERE status = 'PENDING'"
        )["c"].iloc[0]
        if pending_review:
            console.print(f"  [yellow]{pending_review}[/yellow] extractions awaiting review")


@app.command()
def stock(
    symbol: str = typer.Argument(..., help="NSE symbol or ISIN"),
    as_of: str = typer.Option(None, "--as-of", help="Decision date (default: today)"),
    index_name: str = typer.Option(settings.default_index, "--index"),
    min_coverage: float = typer.Option(0.5, "--min-coverage"),
    fill: bool = typer.Option(
        False, "--fill", help="Run the steps that close the gaps, then re-score"
    ),
    paid: bool = typer.Option(
        False, "--paid", help="With --fill, also run steps that spend money"
    ),
) -> None:
    """What data we hold for one company, what is missing, and what fills it.

    `status` counts rows across the whole universe; this is the per-company
    view — which factors are computable, which are blocked and on what, and
    whether a scoring run today would produce a signal at all.
    """
    from stockanalysis.factors.composite import ScoringConfig
    from stockanalysis.serve import readiness as rd

    date = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    cfg = ScoringConfig(min_coverage=min_coverage)

    with open_db(read_only=not fill) as db:
        isin = rd.resolve(db, symbol)
        if isin is None:
            console.print(
                f"[red]{symbol.upper()} is not in `instruments`.[/red] "
                f"Run `seed-universe` first, or check the NSE symbol."
            )
            raise typer.Exit(1)

        report = rd.readiness(db, isin, date, index_name=index_name, config=cfg)
        _print_readiness(report)

        if not fill:
            return

        if not report.gaps:
            console.print("\n[green]No gaps to fill.[/green] Re-scoring anyway.")
        steps = _fill_steps(report, paid)
        console.print(
            f"\n[bold]Filling gaps[/bold] — steps: {', '.join(steps)}\n"
        )
        _run_fill(db, report.symbol or isin, steps, index_name, date, min_coverage)

        console.print("\n[bold]After the run[/bold]")
        _print_readiness(
            rd.readiness(db, isin, date, index_name=index_name, config=cfg)
        )


def _fill_steps(report, paid: bool) -> list[str]:
    """Gap-closing steps plus `score`, with paid steps dropped unless asked for.

    Scoring is always appended: the point of filling a gap is to get a signal
    out of it, and a run that ingests without re-scoring leaves the stored
    signal describing the data as it was before.
    """
    from stockanalysis.run.steps import PAID, STEPS_BY_KEY

    steps = list(report.next_steps())
    if not paid:
        dropped = [k for k in steps if STEPS_BY_KEY[k].cost == PAID]
        steps = [k for k in steps if k not in dropped]
        if dropped:
            console.print(
                f"\n[yellow]Skipping paid step(s):[/yellow] {', '.join(dropped)}. "
                f"Re-run with --paid to include them."
            )
    return [*steps, "score"]


def _run_fill(
    db,
    symbol: str,
    steps: list[str],
    index_name: str,
    as_of: dt.date,
    min_coverage: float,
) -> None:
    from stockanalysis.run.runner import run_now
    from stockanalysis.run.steps import RunOptions, company_plan

    plan = company_plan(
        symbol,
        steps,
        RunOptions(index_name=index_name, as_of=as_of, min_coverage=min_coverage),
    )
    colours = {"warn": "yellow", "error": "red"}

    def show(event) -> None:
        colour = colours.get(event.level)
        text = f"  {event.message}" if event.step else event.message
        console.print(f"[{colour}]{text}[/{colour}]" if colour else text)

    job = run_now(plan, db=db, on_event=show)
    if job.state.value == "failed":
        console.print(f"[red]{job.error}[/red]")


HAVE_MARK = {"PRESENT": "[green]have[/green]",
             "PARTIAL": "[yellow]partial[/yellow]",
             "ABSENT": "[red]missing[/red]"}

FLAG_MARK = {"TRIPPED": "[red]TRIPPED[/red]",
             "CLEAR": "[green]clear[/green]",
             "UNKNOWN": "[yellow]unknown[/yellow]"}


def _print_readiness(report) -> None:
    from stockanalysis.serve.readiness import DATASETS_BY_KEY

    membership = (
        f"{report.index_name} member"
        if report.in_universe
        else f"[yellow]not in {report.index_name}[/yellow] on this date"
    )
    console.print(
        f"\n[bold]{report.name}[/bold] ({report.symbol or '—'})  {report.isin}\n"
        f"{report.sector or 'unclassified'} · {membership} · "
        f"decision date {report.as_of}"
    )

    table = Table(title="Data we hold")
    table.add_column("Source")
    table.add_column("")
    table.add_column("What we hold")
    table.add_column("What is missing")
    table.add_column("Blocks", justify="right")
    for source in report.sources:
        table.add_row(
            source.label,
            HAVE_MARK.get(source.have.value, source.have.value),
            source.detail,
            source.gap or "-",
            str(len(source.blocks)) if source.blocks else "-",
        )
    console.print(table)

    verdict = (
        "[green]enough to score[/green]"
        if report.scorable
        else f"[yellow]below the {report.min_coverage:.0%} floor — "
             f"a run today would leave this company unscored[/yellow]"
    )
    console.print(f"\nModel coverage [bold]{report.coverage:.0%}[/bold] — {verdict}")

    families = Table(title="Coverage by family")
    families.add_column("Family")
    families.add_column("Weight", justify="right")
    families.add_column("Factors", justify="right")
    families.add_column("Measured", justify="right")
    for fam in report.families:
        colour = "green" if fam.covered >= 0.999 else (
            "yellow" if fam.covered > 0 else "red"
        )
        families.add_row(
            fam.family,
            f"{fam.weight:.0%}",
            f"{fam.measured}/{fam.total}",
            f"[{colour}]{fam.covered:.0%}[/{colour}]",
        )
    console.print(families)

    blocked = [f for f in report.factors if not f.computable]
    if blocked:
        missing = Table(title=f"Not computable ({len(blocked)} factors)")
        missing.add_column("Factor")
        missing.add_column("Family")
        missing.add_column("Weight", justify="right")
        missing.add_column("Why")
        for f in sorted(blocked, key=lambda x: -x.weight):
            missing.add_row(f.name, f.family, f"{f.weight:.1%}", f.reason)
        console.print(missing)

    flags = Table(title="Red flags")
    flags.add_column("Flag")
    flags.add_column("")
    flags.add_column("Needs")
    for flag in report.flags:
        needs = "-"
        if not flag.reachable:
            needs = "no source ingests this — cannot be cleared"
        elif flag.blocked_by:
            needs = ", ".join(
                DATASETS_BY_KEY[k].label.lower() for k in flag.blocked_by
            )
        flags.add_row(flag.name, FLAG_MARK.get(flag.state, flag.state), needs)
    console.print(flags)

    if report.stored_as_of is None:
        console.print("\n[yellow]No signal has ever been stored[/yellow] for this company.")
    else:
        score = (
            f"{report.stored_score:.1f}/100"
            if report.stored_score is not None
            else "unscored"
        )
        staleness = (
            f" [yellow](computed {report.stored_as_of}, "
            f"{(report.as_of - report.stored_as_of).days} days before the "
            f"decision date)[/yellow]"
            if report.stale_signal
            else ""
        )
        console.print(
            f"\nLast stored signal: [bold]{report.stored_signal or 'none'}[/bold] "
            f"{score} · {report.stored_version or 'unversioned'}{staleness}"
        )

    steps = report.next_steps()
    if steps:
        console.print(
            f"\n[bold]Next:[/bold] stockanalysis stock {report.symbol} --fill\n"
            f"  equivalently: stockanalysis update {report.symbol} "
            f"--steps {','.join([*steps, 'score'])}"
        )
    else:
        console.print(
            f"\n[green]Every source is complete.[/green] Re-score with "
            f"`stockanalysis update {report.symbol} --steps score`."
        )


# ======================================================================
# Pipeline runs — the same steps the dashboard's Run page drives
# ======================================================================


@app.command("update")
def update_cmd(
    symbol: str = typer.Argument(
        None, help="NSE symbol or ISIN. Omit to update the whole universe."
    ),
    steps: str = typer.Option(
        None,
        "--steps",
        help="Comma-separated step keys. Default: every step that does not "
             "cost money. `--steps list` prints them.",
    ),
    index_name: str = typer.Option(settings.default_index, "--index"),
    as_of: str = typer.Option(None, "--as-of", help="Decision date for scoring"),
    extraction_limit: int = typer.Option(3, "--extract-limit"),
    min_coverage: float = typer.Option(0.5, "--min-coverage"),
) -> None:
    """Run the ingest → extract → score pipeline as one job.

    The headless form of the dashboard's Run page: same steps, same order, same
    reporting. Useful on its own, and it is what makes the pipeline testable
    without a browser.
    """
    from stockanalysis.run.events import StepState
    from stockanalysis.run.runner import run_now
    from stockanalysis.run.steps import (
        PAID,
        RunOptions,
        available_steps,
        company_plan,
        universe_plan,
    )

    scope = "company" if symbol else "universe"

    if steps == "list":
        table = Table(title=f"Steps for a {scope} run")
        table.add_column("Key")
        table.add_column("Step")
        table.add_column("Cost")
        table.add_column("Default")
        for spec in available_steps(scope):
            table.add_row(
                spec.key,
                spec.label,
                "[red]money[/red]" if spec.cost == PAID else spec.cost,
                "on" if spec.default_on else "[dim]off[/dim]",
            )
        console.print(table)
        return

    keys = [s.strip() for s in steps.split(",") if s.strip()] if steps else None
    options = RunOptions(
        index_name=index_name,
        extraction_limit=extraction_limit,
        min_coverage=min_coverage,
        as_of=dt.date.fromisoformat(as_of) if as_of else None,
    )

    try:
        plan = (
            company_plan(symbol, keys, options)
            if symbol
            else universe_plan(keys, options)
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    colours = {"warn": "yellow", "error": "red"}

    def show(event) -> None:
        colour = colours.get(event.level)
        text = f"  {event.message}" if event.step else event.message
        console.print(f"[{colour}]{text}[/{colour}]" if colour else text)

    settings.ensure_dirs()
    with open_db() as db:
        job = run_now(plan, db=db, on_event=show)

    table = Table(title=job.title)
    table.add_column("Step")
    table.add_column("Result")
    table.add_column("Took", justify="right")
    table.add_column("Detail")
    marks = {
        StepState.DONE: "[green]done[/green]",
        StepState.SKIPPED: "[yellow]skipped[/yellow]",
        StepState.FAILED: "[red]FAILED[/red]",
        StepState.CANCELLED: "[yellow]cancelled[/yellow]",
        StepState.PENDING: "[dim]not reached[/dim]",
    }
    for record in job.steps:
        detail = record.error or record.message or ", ".join(
            f"{k} {v}" for k, v in record.summary.items()
        )
        table.add_row(
            record.label,
            marks.get(record.state, record.state.value),
            f"{record.duration_seconds:.0f}s" if record.duration_seconds else "-",
            detail[:70],
        )
    console.print(table)

    if job.state.value == "failed":
        console.print(f"[red]{job.error}[/red]")
        raise typer.Exit(1)


@app.command("serve-api")
def serve_api(
    # Loopback by default. The API is an unauthenticated read surface over the
    # whole research database; binding it to every interface should be a
    # deliberate act, not what happens when you accept the defaults.
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8000, "--port", help="Port"),
) -> None:
    """Serve signals and factor data over HTTP (read-only)."""
    import uvicorn

    from stockanalysis.serve.api import app as api_app

    console.print(f"[green]Starting API[/green] on http://{host}:{port}")
    console.print(f"  http://{host}:{port}/docs — interactive API docs")
    uvicorn.run(api_app, host=host, port=port)


@app.command()
def dashboard(
    port: int = typer.Option(8501, "--port", help="Port"),
) -> None:
    """Open the Streamlit dashboard in a browser."""
    import subprocess
    import sys

    dashboard_path = Path(__file__).parent / "serve" / "dashboard.py"
    console.print(f"[green]Starting dashboard[/green] on http://localhost:{port}")

    result = subprocess.run(
        [
            sys.executable, "-m", "streamlit", "run", str(dashboard_path),
            "--server.port", str(port),
        ],
        cwd=Path.cwd(),
    )
    raise typer.Exit(result.returncode)


if __name__ == "__main__":
    app()
