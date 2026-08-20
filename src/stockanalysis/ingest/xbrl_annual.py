"""Annual financials from the exchange's own XBRL, with no model in the loop.

This is the cheap route to `fundamentals_annual`. NSE attaches a tagged Ind AS
instance to the Q4/audited-annual results filing, and — contrary to what this
project assumed for two phases — that instance carries the balance sheet and the
cash flow statement alongside the P&L. `xbrl.parse_annual_xbrl` reads it; this
module decides *which* filing to read and turns the result into a row.

WHY THIS IS PREFERABLE TO THE PDF PATH, NOT MERELY CHEAPER
----------------------------------------------------------
The figures are tagged rather than typeset, so there is no column to misread, no
consolidated-versus-standalone confusion, and no confidence score to compute.
The LLM path remains for what XBRL genuinely cannot supply — contingent
liabilities, the auditor's qualification text, and any company-year with no
usable instance.

THE KNOWLEDGE DATE MOVES EARLIER, AND THAT IS CORRECT
-----------------------------------------------------
`nse_fundamentals.apply_results_filing_index` deliberately refuses to put a
results-filing date on annual figures, reasoning that operating cash flow and
the auditor's opinion "only exist in the report" and so cannot be known when the
results are filed.

For an XBRL-sourced row that reasoning no longer applies: the operating cash
flow and the audit declaration are *in the results filing*, tagged, on the day
it was broadcast. So these rows take the filing's broadcast date — typically
some six weeks after year end rather than the six-month AGM deadline the PDF
path must assume. That is earlier *legitimate* knowledge, not lookahead, and it
is the one case where moving a knowledge date earlier is defensible. Rows from
the PDF path keep the later date, which is why `source` distinguishes them.

WHY THE BASIS IS CHOSEN PER COMPANY, NOT PER FILING
---------------------------------------------------
A company files consolidated and standalone results as separate entries with
separate instances. Consolidated is what the factor model wants, but a company
with no subsidiaries files only standalone, and mixing the two across years
turns a CAGR into a measurement of the change of basis. So the basis is settled
once per ISIN — the same rule `Database.as_of_fundamentals_history` already
applies on the read side.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.extract.schema import AnnualReportExtraction
from stockanalysis.extract.validate import ValidationReport, validate
from stockanalysis.ingest.xbrl import AnnualFacts, NotAnnualFiling, parse_annual_xbrl

log = logging.getLogger(__name__)

SOURCE_XBRL = "XBRL"

# What this path calls itself in `extraction_attempts`, the table it shares with
# the paid one. Named rather than repeated because `readiness` reads it back to
# tell a filing that cannot be parsed from one not yet reached — the difference
# between offering the annual report and offering another crawl.
XBRL_MODEL = "xbrl"

# `nsearchives` serves the instance documents and, unlike the RSS outlets in
# `rss.py`, refuses a bare `python-requests` agent. Measured against a live
# fetch, not guessed.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class AnnualFilingRef:
    """A results filing that should carry a full year of tagged financials."""

    isin: str
    symbol: str | None
    period_end: dt.date
    broadcast_date: dt.date
    is_consolidated: bool | None
    xbrl_url: str

    @property
    def fiscal_year(self) -> int:
        """The year the period ends in.

        Matches what `fundamentals_annual` already holds — a March-2025 year end
        is FY2025, and so is ABB's December-2025 one. Deriving it any other way
        would put the same company-year under two labels depending on which path
        wrote the row, and the primary key is (isin, fiscal_year, basis).
        """
        return self.period_end.year

    @property
    def basis(self) -> str:
        return "CONSOLIDATED" if self.is_consolidated else "STANDALONE"


# SEBI LODR allows 60 days after the year end for audited annual results. Used
# only where NSE's index carries no broadcast timestamp: the filing is known to
# exist, and dropping the year over a missing date would cost a company-year
# that the deadline can date conservatively instead.
_ANNUAL_DEADLINE_DAYS = 60


def pending_annual_filings(
    db: Database,
    isins: list[str] | None = None,
    only_missing: bool = True,
) -> list[AnnualFilingRef]:
    """Q4/annual results filings with an XBRL attachment, one basis per ISIN.

    Reads `results_filings`, the stored filing index. It used to read
    `fundamentals_quarterly`, which was the bug that capped this path at one
    fiscal year per company: that table's rows come from `results_comparison`
    and span only about five quarters, so it holds exactly one March quarter and
    every earlier year's filing — fetched, and carrying an XBRL link — matched
    no row and was dropped. The index table is populated directly by
    `record_results_filings` and reaches back as far as the index is walked.
    """
    where = [
        "f.relating_to = 'Fourth Quarter'",
        "f.xbrl_url IS NOT NULL",
        "f.xbrl_url LIKE '%.xml'",
    ]
    params: list = []
    if isins:
        where.append(f"f.isin IN ({', '.join('?' for _ in isins)})")
        params.extend(isins)

    rows = db.query(
        f"""
        SELECT f.isin, i.nse_symbol, f.period_end_date, f.broadcast_date,
               f.is_consolidated, f.xbrl_url
        FROM results_filings f
        LEFT JOIN instruments i ON i.isin = f.isin
        WHERE {' AND '.join(where)}
        ORDER BY f.isin, f.period_end_date DESC
        """,
        params,
    )
    if rows.empty:
        return []

    refs = []
    for r in rows.itertuples(index=False):
        period_end = pd.Timestamp(r.period_end_date).date()
        refs.append(
            AnnualFilingRef(
                isin=r.isin,
                symbol=r.nse_symbol,
                period_end=period_end,
                broadcast_date=(
                    period_end + dt.timedelta(days=_ANNUAL_DEADLINE_DAYS)
                    if pd.isna(r.broadcast_date)
                    else pd.Timestamp(r.broadcast_date).date()
                ),
                is_consolidated=(
                    None if pd.isna(r.is_consolidated) else bool(r.is_consolidated)
                ),
                xbrl_url=str(r.xbrl_url),
            )
        )
    refs = _one_basis_per_isin(refs)

    if only_missing:
        refs = [r for r in refs if not _already_have(db, r) and not _refused(db, r)]
    return refs


def _one_basis_per_isin(refs: list[AnnualFilingRef]) -> list[AnnualFilingRef]:
    """Consolidated where the company files it, standalone otherwise — never both."""
    consolidated: set[str] = {r.isin for r in refs if r.is_consolidated}
    out = [
        r for r in refs
        if (r.is_consolidated if r.isin in consolidated else not r.is_consolidated)
    ]
    # One filing per company-year even so: a company can refile the same period.
    seen: set[tuple[str, int]] = set()
    unique = []
    for r in sorted(out, key=lambda r: (r.isin, r.period_end, r.broadcast_date)):
        key = (r.isin, r.fiscal_year)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def _already_have(db: Database, ref: AnnualFilingRef) -> bool:
    found = db.query(
        "SELECT 1 FROM fundamentals_annual "
        "WHERE isin = ? AND fiscal_year = ? AND basis = ? AND source = ?",
        [ref.isin, ref.fiscal_year, ref.basis, SOURCE_XBRL],
    )
    return not found.empty


def _attempt_id(ref: AnnualFilingRef) -> str:
    """Stable per company-year-basis, so a re-run replaces rather than piles up."""
    return f"{ref.isin}-FY{ref.fiscal_year}-{ref.basis}-xbrl"


def _refused(db: Database, ref: AnnualFilingRef) -> bool:
    """Whether this filing has already been read and found unusable.

    Without this the free path has no memory of what it cannot do. A bank's
    instance tags no revenue line and never will, so the filing stays pending
    for ever — and `readiness` goes on offering the free step for a gap that
    only the annual report can close. The failure is recorded rather than the
    filing being marked bad in place: `only_missing=False` still returns it, so
    a fix to the parser can be re-run over everything it previously refused.
    """
    found = db.query(
        "SELECT 1 FROM extraction_attempts "
        "WHERE attempt_id = ? AND error IS NOT NULL",
        [_attempt_id(ref)],
    )
    return not found.empty


def _record_refusal(db: Database, ref: AnnualFilingRef, error: str) -> None:
    """Log a filing this path cannot use, in the table the paid path already uses.

    Structural failures only — the caller decides. A fetch that timed out or was
    rate-limited says nothing about the filing, and retiring a company-year over
    a transient 429 would quietly move it to the paid path.
    """
    db.upsert_df(
        "extraction_attempts",
        pd.DataFrame([{
            "attempt_id": _attempt_id(ref),
            "filing_id": ref.xbrl_url,
            "isin": ref.isin,
            "fiscal_year": ref.fiscal_year,
            "model": XBRL_MODEL,
            "run_label": "xbrl-annual",
            "cost_usd": 0.0,
            "confidence": 0.0,
            "error": error,
            "created_at": dt.datetime.now(),
        }]),
        ["attempt_id"],
    )


# ----------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------


def cache_path(url: str) -> Path:
    """Where an instance is kept once downloaded.

    Cached by the filing's own filename, which NSE makes unique per document.
    Re-running the ingest should not re-download: these are immutable published
    filings, and the endpoint is the part of this pipeline most likely to
    rate-limit.
    """
    return settings.data_dir / "cache" / "xbrl" / url.rsplit("/", 1)[-1]


def fetch_instance(url: str, session=None) -> bytes:
    """The instance document, from cache when we already hold it."""
    path = cache_path(url)
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()

    import requests

    getter = session or requests
    response = getter.get(url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return response.content


# ----------------------------------------------------------------------
# Persist
# ----------------------------------------------------------------------


def to_row(ref: AnnualFilingRef, facts: AnnualFacts) -> dict:
    """One `fundamentals_annual` row from one parsed instance.

    `fiscal_year` comes from the filing, not the instance, for the same reason
    the PDF path takes it from the filing: an instance that reports a different
    period should surface as a validator failure rather than land quietly in
    another year's row.
    """
    v = facts.to_crore()
    return {
        "isin": ref.isin,
        "fiscal_year": ref.fiscal_year,
        "period_end_date": facts.period_end or ref.period_end,
        # See the module docstring — the results broadcast, not the AGM deadline.
        "filing_date": ref.broadcast_date,
        "basis": ref.basis,
        "revenue": v.get("revenue"),
        "other_income": v.get("other_income"),
        "total_expenses": v.get("total_expenses"),
        "total_income": v.get("total_income"),
        "ebitda": None,  # not tagged in this taxonomy; never derived
        "depreciation": v.get("depreciation"),
        "profit_before_tax": v.get("profit_before_tax"),
        "share_of_associates": v.get("share_of_associates"),
        "non_controlling_interest": v.get("non_controlling_interest"),
        "pat": v.get("pat"),
        "eps": v.get("eps_basic"),
        "ocf": v.get("ocf"),
        "fcf": _fcf(v),
        "capex": v.get("capex"),
        "total_assets": v.get("total_assets"),
        "total_equity": v.get("total_equity"),
        "total_liabilities": v.get("total_liabilities"),
        "total_debt": v.get("total_debt"),
        "cash": v.get("cash"),
        "interest_expense": v.get("interest_expense"),
        "tax_expense": v.get("tax_expense"),
        # No element exists for this in the taxonomy. NULL means unknown, and
        # the red flag that reads it must treat it as unknown rather than clean.
        "contingent_liabilities": None,
        "auditor_opinion": _opinion(facts),
        # Tagged data read by a parser, not a model's reading of a page. There
        # is no confidence to score, and pretending otherwise would put these
        # rows in the review queue alongside genuinely uncertain ones.
        "extraction_confidence": 1.0,
        "source": SOURCE_XBRL,
        "extraction_model": "xbrl",
        "source_filing_id": None,
        "extracted_at": dt.datetime.now(),
    }


def _fcf(values: dict) -> float | None:
    ocf, capex = values.get("ocf"), values.get("capex")
    return None if ocf is None or capex is None else ocf - capex


def _opinion(facts: AnnualFacts) -> str | None:
    """The audit declaration, mapped onto the schema's opinion vocabulary.

    The taxonomy carries a free-text declaration rather than a classification,
    and in practice it says some variant of "unmodified". Anything else is not
    confidently a qualification, so it is left unset rather than guessed at —
    a wrongly-clean opinion would silently clear the auditor red flag.
    """
    note = (facts.opinion_note or "").strip().lower()
    if not note:
        return None
    if "unmodified" in note:
        return "UNMODIFIED"
    if "qualified" in note or "qualification" in note:
        return "QUALIFIED"
    return None


def persist(db: Database, row: dict) -> None:
    db.upsert_df(
        "fundamentals_annual", pd.DataFrame([row]), ["isin", "fiscal_year", "basis"]
    )


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


@dataclass
class IngestResult:
    ref: AnnualFilingRef
    row: dict | None = None
    error: str | None = None
    report: ValidationReport | None = None
    # Whether the filing was actually read and judged unusable, as opposed to
    # never reached. Both leave `error` set and no row, and the caller has to
    # tell them apart: a refusal is this company's answer — it needs the annual
    # report — while a 429 is a verdict on the network and nothing else.
    refused: bool = False

    @property
    def ok(self) -> bool:
        return self.row is not None


def ingest_annual_xbrl(
    db: Database,
    refs: list[AnnualFilingRef],
    progress: Callable[[int, int, IngestResult], None] | None = None,
    session=None,
) -> list[IngestResult]:
    """Fetch, parse and persist each filing. One row per company-year at most.

    A filing that turns out not to be annual, or that parses without the fields
    the factor model needs, is reported and skipped rather than written — the
    LLM path is what covers those, and a half-populated row would stop it from
    running.
    """
    out: list[IngestResult] = []
    for i, ref in enumerate(refs, start=1):
        result = _ingest_one(db, ref, session)
        out.append(result)
        if progress:
            progress(i, len(refs), result)
        if not cache_path(ref.xbrl_url).exists():
            time.sleep(settings.request_delay_seconds)
    return out


def _ingest_one(db: Database, ref: AnnualFilingRef, session=None) -> IngestResult:
    def refuse(error: str) -> IngestResult:
        """Read, and unusable. Recorded so this filing is not offered again."""
        _record_refusal(db, ref, error)
        return IngestResult(ref, error=error, refused=True)

    try:
        raw = fetch_instance(ref.xbrl_url, session=session)
    except Exception as e:  # noqa: BLE001 - unofficial endpoint, many failure modes
        # Not recorded: the filing was never read, so nothing is known about it.
        return IngestResult(ref, error=f"fetch failed: {e}")

    try:
        facts = parse_annual_xbrl(raw)
    except NotAnnualFiling as e:
        return refuse(f"not an annual filing: {e}")
    except Exception as e:  # noqa: BLE001 - malformed instances are common enough
        return refuse(f"parse failed: {type(e).__name__}: {e}")

    row = to_row(ref, facts)
    missing = [f for f in REQUIRED_FOR_FACTORS if row.get(f) is None]
    if missing:
        return refuse(
            f"incomplete: {', '.join(missing)} not tagged in the instance"
        )

    # The same arithmetic the PDF path is held to. Tagged data removes the risk
    # of misreading a *number*, not the risk of reading the wrong *context* —
    # and a quarter's P&L against a year-end balance sheet is exactly the shape
    # of error the profit and balance-sheet identities catch.
    report = validate(_as_extraction(row), fiscal_year=ref.fiscal_year)
    if report.hard_failures:
        return refuse(f"failed validation: {report.reasons}")
    row["extraction_confidence"] = report.confidence

    persist(db, row)
    return IngestResult(ref, row=row, report=report)


def _as_extraction(row: dict) -> AnnualReportExtraction:
    """A persisted row in the shape the validators read.

    Amounts are already in crore, so the unit is declared as CRORE and
    `to_crore` inside the validator becomes the identity. Reusing the existing
    validator this way is deliberate: a second implementation of the same
    identities would drift from the first, and the two paths would start
    disagreeing about what a valid statement is.
    """
    return AnnualReportExtraction(
        period_end_date=row["period_end_date"],
        basis=row["basis"],
        reporting_unit="CRORE",
        currency="INR",
        revenue=row["revenue"],
        other_income=row["other_income"],
        total_income=row.get("total_income"),
        total_expenses=row["total_expenses"],
        depreciation=row["depreciation"],
        interest_expense=row["interest_expense"],
        profit_before_tax=row["profit_before_tax"],
        tax_expense=row["tax_expense"],
        share_of_associates=row["share_of_associates"],
        non_controlling_interest=row["non_controlling_interest"],
        pat=row["pat"],
        eps_basic=row["eps"],
        total_assets=row["total_assets"],
        total_equity=row["total_equity"],
        total_liabilities=row["total_liabilities"],
        total_debt=row["total_debt"],
        cash=row["cash"],
        ocf=row["ocf"],
        capex=row["capex"],
        auditor_opinion=row["auditor_opinion"],
    )


# Without these the row buys nothing the free quarterly source does not already
# give, and leaving it in place would suppress the LLM fallback that could.
REQUIRED_FOR_FACTORS = ("revenue", "pat", "total_assets", "total_equity", "ocf")
