"""Orchestration: filing -> located pages -> extraction -> validation -> row.

The one rule this module exists to enforce:

    `fundamentals_annual.filing_date` comes from `filings.broadcast_date`.
    Never from the extraction.

The model reads a date off the front of the report — the balance sheet date, the
signing date, the AGM notice date — and any of them is a plausible-looking
filing_date. Letting one through is how a backtest ends up reading FY2024
figures in April 2024, and the resulting alpha looks real right up until it is
traded. So the extraction's `period_end_date` describes the period, the filing's
`broadcast_date` decides when it became knowable, and the two are never allowed
to swap roles.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.extract.claude import ClaudeExtractor, ExtractionJob, ExtractionResult
from stockanalysis.extract.factory import make_extractor
from stockanalysis.extract.locator import SectionLocatorError, locate_sections
from stockanalysis.extract.schema import to_crore
from stockanalysis.extract.validate import ValidationReport, derived_fcf, validate
from stockanalysis.ingest.nse_fundamentals import quarters_for_fiscal_year

log = logging.getLogger(__name__)


@dataclass
class FilingRow:
    filing_id: str
    isin: str
    symbol: str
    company: str
    fiscal_year: int
    period_end: dt.date
    broadcast_date: dt.date
    broadcast_date_source: str
    local_path: str


def pending_filings(
    db: Database,
    limit: int | None = None,
    isins: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    only_unextracted: bool = True,
    model: str | None = None,
) -> list[FilingRow]:
    """Annual-report filings on disk, optionally only those not yet extracted."""
    where = ["f.doc_type = 'ANNUAL_REPORT'", "f.local_path IS NOT NULL"]
    params: list = []

    if isins:
        where.append(f"f.isin IN ({', '.join('?' for _ in isins)})")
        params.extend(isins)
    if fiscal_years:
        where.append(f"f.fiscal_year IN ({', '.join('?' for _ in fiscal_years)})")
        params.extend(fiscal_years)
    if only_unextracted:
        # An attempt that errored does not count as done — retrying a transient
        # API failure should not require deleting rows by hand.
        clause = (
            "NOT EXISTS (SELECT 1 FROM extraction_attempts a "
            "WHERE a.filing_id = f.filing_id AND a.error IS NULL"
        )
        if model:
            clause += " AND a.model = ?"
            params.append(model)
        where.append(clause + ")")

    sql = f"""
        SELECT f.filing_id, f.isin, i.nse_symbol, i.name, f.fiscal_year,
               f.period_end, f.broadcast_date, f.broadcast_date_source, f.local_path
        FROM filings f
        JOIN instruments i ON i.isin = f.isin
        WHERE {' AND '.join(where)}
        ORDER BY f.isin, f.fiscal_year DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    df = db.query(sql, params)
    return [
        FilingRow(
            filing_id=r.filing_id,
            isin=r.isin,
            symbol=r.nse_symbol,
            company=r.name,
            fiscal_year=int(r.fiscal_year),
            period_end=r.period_end,
            broadcast_date=r.broadcast_date,
            broadcast_date_source=r.broadcast_date_source,
            local_path=r.local_path,
        )
        for r in df.itertuples(index=False)
    ]


def build_job(filing: FilingRow) -> ExtractionJob:
    """Run the section locator and package the result for the API.

    Propagates `SectionLocatorError` — a report we cannot narrow is a review
    case, and guessing at 60 arbitrary pages of 300 would be worse than failing.
    """
    path = Path(filing.local_path)
    if not path.exists():
        raise FileNotFoundError(f"{path} is registered in filings but missing on disk")

    located = locate_sections(path)
    return ExtractionJob(
        filing_id=filing.filing_id,
        isin=filing.isin,
        symbol=filing.symbol,
        company=filing.company,
        fiscal_year=filing.fiscal_year,
        pdf_bytes=located.pdf_bytes,
        pages_sent=located.page_count,
        source_pages=located.page_range_str(),
    )


def persist(
    db: Database,
    filing: FilingRow,
    result: ExtractionResult,
    report: ValidationReport | None,
    run_label: str = "adhoc",
) -> str:
    """Record the attempt, and the extracted row if it is good enough.

    Returns the attempt_id. Always writes to `extraction_attempts`, including on
    failure — an extraction that errored is data about the pipeline, and losing
    it means re-running the same crawl to rediscover the same problem.
    """
    attempt_id = uuid.uuid4().hex[:16]
    confidence = report.confidence if report else 0.0

    db.upsert_df(
        "extraction_attempts",
        pd.DataFrame(
            [
                {
                    "attempt_id": attempt_id,
                    "filing_id": filing.filing_id,
                    "isin": filing.isin,
                    "fiscal_year": filing.fiscal_year,
                    "model": result.model,
                    "run_label": run_label,
                    "mode": result.mode,
                    "pages_sent": result.job.pages_sent,
                    "source_pages": result.job.source_pages,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cache_read_tokens": result.usage.cache_read_tokens,
                    "cache_creation_tokens": result.usage.cache_creation_tokens,
                    "cost_usd": result.cost_usd(),
                    "latency_seconds": result.latency_seconds,
                    "confidence": confidence,
                    "checks_json": json.dumps(report.as_dict()) if report else None,
                    "payload_json": (
                        result.payload.model_dump_json() if result.payload else None
                    ),
                    "error": result.error,
                    "created_at": dt.datetime.now(),
                }
            ]
        ),
        ["attempt_id"],
    )

    if result.payload is None or report is None:
        _queue_review(db, attempt_id, filing, result.model, 0.0, result.error or "no payload")
        return attempt_id

    if confidence >= settings.min_persist_confidence:
        _write_fundamentals(db, filing, result, report, attempt_id)

    if confidence < 1.0:
        _queue_review(db, attempt_id, filing, result.model, confidence, report.reasons)

    return attempt_id


def _write_fundamentals(
    db: Database,
    filing: FilingRow,
    result: ExtractionResult,
    report: ValidationReport,
    attempt_id: str,
) -> None:
    payload = result.payload
    v = to_crore(payload)

    row = {
        "isin": filing.isin,
        # From the filing, not the extraction: the filing is what we asked for,
        # and a model that misread the year should surface as a validator
        # failure rather than quietly land in the wrong fiscal year's row.
        "fiscal_year": filing.fiscal_year,
        "period_end_date": payload.period_end_date or filing.period_end,
        # THE point-in-time contract. See the module docstring.
        "filing_date": filing.broadcast_date,
        "basis": payload.basis or "UNKNOWN",
        "revenue": v.get("revenue"),
        "other_income": v.get("other_income"),
        "total_expenses": v.get("total_expenses"),
        "ebitda": v.get("ebitda"),
        "depreciation": v.get("depreciation"),
        "profit_before_tax": v.get("profit_before_tax"),
        "pat": v.get("pat"),
        "eps": v.get("eps_basic"),
        "ocf": v.get("ocf"),
        "fcf": derived_fcf(payload),
        "capex": v.get("capex"),
        "total_assets": v.get("total_assets"),
        "total_equity": v.get("total_equity"),
        "total_liabilities": v.get("total_liabilities"),
        "total_debt": v.get("total_debt"),
        "cash": v.get("cash"),
        "interest_expense": v.get("interest_expense"),
        "tax_expense": v.get("tax_expense"),
        "contingent_liabilities": v.get("contingent_liabilities"),
        "auditor_opinion": payload.auditor_opinion,
        "extraction_confidence": report.confidence,
        "source_filing_id": filing.filing_id,
        "extraction_model": result.model,
        "extraction_attempt_id": attempt_id,
        "extracted_at": dt.datetime.now(),
    }
    db.upsert_df(
        "fundamentals_annual", pd.DataFrame([row]), ["isin", "fiscal_year", "basis"]
    )


def _queue_review(
    db: Database,
    attempt_id: str,
    filing: FilingRow,
    model: str,
    confidence: float,
    reasons: str,
) -> None:
    db.upsert_df(
        "extraction_review",
        pd.DataFrame(
            [
                {
                    "attempt_id": attempt_id,
                    "filing_id": filing.filing_id,
                    "isin": filing.isin,
                    "fiscal_year": filing.fiscal_year,
                    "model": model,
                    "confidence": confidence,
                    "reasons": reasons[:2000],
                    "status": "PENDING",
                    "queued_at": dt.datetime.now(),
                    "resolved_at": None,
                    "notes": None,
                }
            ]
        ),
        ["attempt_id"],
    )


def extract_one(
    db: Database,
    filing: FilingRow,
    extractor: ClaudeExtractor,
    run_label: str = "adhoc",
) -> tuple[ExtractionResult, ValidationReport | None]:
    """Locate, extract, validate and persist a single filing."""
    try:
        job = build_job(filing)
    except (SectionLocatorError, FileNotFoundError) as e:
        result = ExtractionResult(
            job=ExtractionJob(
                filing_id=filing.filing_id,
                isin=filing.isin,
                symbol=filing.symbol,
                company=filing.company,
                fiscal_year=filing.fiscal_year,
                pdf_bytes=b"",
            ),
            model=extractor.model,
            mode="SYNC",
            error=f"{type(e).__name__}: {e}",
        )
        persist(db, filing, result, None, run_label)
        return result, None

    result = extractor.extract(job)
    report = _validate_result(db, filing, result)
    persist(db, filing, result, report, run_label)
    return result, report


def _validate_result(
    db: Database, filing: FilingRow, result: ExtractionResult
) -> ValidationReport | None:
    if result.payload is None:
        return None
    quarters = quarters_for_fiscal_year(db, filing.isin, filing.fiscal_year)
    return validate(result.payload, fiscal_year=filing.fiscal_year, nse_quarterly=quarters)


def run_extraction(
    db: Database,
    filings: list[FilingRow],
    extractor: ClaudeExtractor | None = None,
    run_label: str = "backfill",
    progress: callable | None = None,
) -> list[tuple[ExtractionResult, ValidationReport | None]]:
    """Extract a list of filings synchronously."""
    extractor = extractor or make_extractor(settings.extraction_model)
    out = []
    for i, filing in enumerate(filings, start=1):
        result, report = extract_one(db, filing, extractor, run_label)
        out.append((result, report))
        if progress:
            progress(i, len(filings), filing, result, report)
        else:
            log.info(
                "[%d/%d] %s FY%d: %s",
                i, len(filings), filing.symbol, filing.fiscal_year,
                result.error or f"confidence={report.confidence if report else 0.0}",
            )
    return out


# ----------------------------------------------------------------------
# Batch path
# ----------------------------------------------------------------------


def submit_batch(
    db: Database,
    filings: list[FilingRow],
    extractor: ClaudeExtractor | None = None,
) -> tuple[str, list[ExtractionJob], list[tuple[FilingRow, str]]]:
    """Locate every filing and submit them as one batch.

    Returns the batch id, the submitted jobs, and the filings that could not be
    prepared. Locator failures are persisted immediately so they show up in the
    review queue rather than vanishing between submit and collect.
    """
    extractor = extractor or ClaudeExtractor()
    jobs: list[ExtractionJob] = []
    skipped: list[tuple[FilingRow, str]] = []

    for filing in filings:
        try:
            jobs.append(build_job(filing))
        except (SectionLocatorError, FileNotFoundError) as e:
            reason = f"{type(e).__name__}: {e}"
            skipped.append((filing, reason))
            persist(
                db,
                filing,
                ExtractionResult(
                    job=ExtractionJob(
                        filing_id=filing.filing_id, isin=filing.isin, symbol=filing.symbol,
                        company=filing.company, fiscal_year=filing.fiscal_year, pdf_bytes=b"",
                    ),
                    model=extractor.model,
                    mode="BATCH",
                    error=reason,
                ),
                None,
                "backfill",
            )

    if not jobs:
        raise RuntimeError("no filings could be prepared for batch submission")

    return extractor.submit_batch(jobs), jobs, skipped


def collect_batch(
    db: Database,
    batch_id: str,
    filings: list[FilingRow],
    extractor: ClaudeExtractor | None = None,
    run_label: str = "backfill",
) -> list[tuple[ExtractionResult, ValidationReport | None]]:
    """Fetch, validate and persist a completed batch.

    The jobs are rebuilt from the filings rather than carried across the
    submit/collect boundary — the locator is deterministic, so this reproduces
    the same `custom_id` set without needing to persist multi-megabyte PDFs
    between two CLI invocations.
    """
    extractor = extractor or ClaudeExtractor()
    by_filing = {f.filing_id: f for f in filings}

    jobs = []
    for filing in filings:
        try:
            jobs.append(build_job(filing))
        except (SectionLocatorError, FileNotFoundError):
            continue  # already persisted as an error at submit time

    out = []
    for result in extractor.collect_batch(batch_id, jobs):
        filing = by_filing[result.job.filing_id]
        report = _validate_result(db, filing, result)
        persist(db, filing, result, report, run_label)
        out.append((result, report))
    return out
