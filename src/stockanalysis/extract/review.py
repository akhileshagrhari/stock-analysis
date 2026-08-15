"""Human review queue for low-confidence extractions.

The queue exists because the alternative is worse in both directions. Dropping
every flagged row silently deletes the companies with unusual reporting formats —
which correlates uncomfortably well with the companies worth being suspicious of.
Accepting every row lets a misread balance sheet propagate into every factor
computed from it, where it is effectively undetectable.

A row reaches the queue when any validator failed. Rows at confidence >= 0.6 are
*also* persisted to `fundamentals_annual` while queued: they are usable but
worth a look. Rows below that are queued only, and no factor can see them until
a human accepts them.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.extract.claude import ExtractionJob, ExtractionResult, Usage
from stockanalysis.extract.pipeline import FilingRow, _write_fundamentals
from stockanalysis.extract.schema import AnnualReportExtraction
from stockanalysis.extract.validate import Check, ValidationReport

log = logging.getLogger(__name__)


def pending(db: Database, limit: int = 50) -> pd.DataFrame:
    """Queued extractions awaiting a decision, worst confidence first."""
    return db.query(
        """
        SELECT r.attempt_id, r.filing_id, r.isin, i.nse_symbol, r.fiscal_year,
               r.model, r.confidence, r.reasons, r.queued_at
        FROM extraction_review r
        LEFT JOIN instruments i ON i.isin = r.isin
        WHERE r.status = 'PENDING'
        ORDER BY r.confidence ASC, r.queued_at ASC
        LIMIT ?
        """,
        [limit],
    )


def detail(db: Database, attempt_id: str) -> dict:
    """Everything a reviewer needs: the extraction, the checks, the source pages."""
    df = db.query(
        """
        SELECT a.*, f.local_path, f.source_url, f.broadcast_date,
               f.broadcast_date_source, i.nse_symbol, i.name
        FROM extraction_attempts a
        JOIN filings f ON f.filing_id = a.filing_id
        LEFT JOIN instruments i ON i.isin = a.isin
        WHERE a.attempt_id = ?
        """,
        [attempt_id],
    )
    if df.empty:
        raise KeyError(f"no extraction attempt {attempt_id!r}")

    row = df.iloc[0].to_dict()
    row["payload"] = json.loads(row["payload_json"]) if row.get("payload_json") else None
    row["checks"] = json.loads(row["checks_json"]) if row.get("checks_json") else None
    return row


def resolve(
    db: Database,
    attempt_id: str,
    status: str,
    notes: str | None = None,
    force_persist: bool = False,
) -> None:
    """Mark a queued extraction ACCEPTED or REJECTED.

    `force_persist` writes an accepted row into `fundamentals_annual` even when
    its confidence was below the persist threshold — the point of a human review
    is that a person can overrule a validator that fired on a legitimately
    unusual report (a bank with no capex line, say). The row keeps its original
    confidence score so the override stays visible downstream.
    """
    status = status.upper()
    if status not in ("ACCEPTED", "REJECTED"):
        raise ValueError(f"status must be ACCEPTED or REJECTED, got {status!r}")

    existing = db.query(
        "SELECT * FROM extraction_review WHERE attempt_id = ?", [attempt_id]
    )
    if existing.empty:
        raise KeyError(f"no review entry for attempt {attempt_id!r}")

    if status == "REJECTED":
        # Pull the row back out of fundamentals so a rejected extraction cannot
        # keep feeding factors. Deleting by attempt id, not by key, so a later
        # good extraction of the same company-year is left alone.
        db.conn.execute(
            "DELETE FROM fundamentals_annual WHERE extraction_attempt_id = ?",
            [attempt_id],
        )

    if status == "ACCEPTED" and force_persist:
        _persist_accepted(db, attempt_id)

    db.conn.execute(
        "UPDATE extraction_review SET status = ?, resolved_at = ?, notes = ? "
        "WHERE attempt_id = ?",
        [status, dt.datetime.now(), notes, attempt_id],
    )


def _persist_accepted(db: Database, attempt_id: str) -> None:
    """Write a below-threshold extraction into fundamentals after human sign-off."""
    row = detail(db, attempt_id)
    if not row.get("payload"):
        raise ValueError(f"attempt {attempt_id} has no payload to persist")

    filing = FilingRow(
        filing_id=row["filing_id"],
        isin=row["isin"],
        symbol=row.get("nse_symbol") or "",
        company=row.get("name") or "",
        fiscal_year=int(row["fiscal_year"]),
        period_end=dt.date(int(row["fiscal_year"]), 3, 31),
        broadcast_date=row["broadcast_date"],
        broadcast_date_source=row["broadcast_date_source"],
        local_path=row["local_path"],
    )

    payload = AnnualReportExtraction.model_validate(row["payload"])
    result = ExtractionResult(
        job=ExtractionJob(
            filing_id=filing.filing_id, isin=filing.isin, symbol=filing.symbol,
            company=filing.company, fiscal_year=filing.fiscal_year, pdf_bytes=b"",
        ),
        model=row["model"],
        mode=row.get("mode") or "SYNC",
        payload=payload,
        usage=Usage(),
    )

    # Rebuild the report so the persisted confidence is the machine's original
    # verdict, not a 1.0 invented by the act of accepting it.
    checks = row.get("checks") or {}
    report = ValidationReport(
        checks=[
            Check(
                name=c["name"], passed=c["passed"], severity=c["severity"],
                detail=c["detail"], skipped=c.get("skipped", False),
            )
            for c in checks.get("checks", [])
        ]
    )
    _write_fundamentals(db, filing, result, report, attempt_id)


def summary(db: Database) -> pd.DataFrame:
    """Queue health: how many are waiting, and how the resolved ones went."""
    return db.query(
        """
        SELECT status, COUNT(*) AS n, ROUND(AVG(confidence), 2) AS avg_confidence
        FROM extraction_review GROUP BY status ORDER BY status
        """
    )
