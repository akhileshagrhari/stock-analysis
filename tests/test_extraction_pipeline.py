"""Pipeline persistence, and the point-in-time contract it has to uphold.

The load-bearing test here is `test_backtest_cannot_see_a_filing_before_it_was
_published`. Phase 0 established that a backtest may only read rows whose
knowledge date is on or before the decision date; phase 1 introduces the first
data source where the knowledge date is *not* the same as the date the numbers
describe. FY2024 figures describe a year ending 31 March 2024 and became public
in September. Wiring `filing_date` to the wrong one of those two dates is a
silent, plausible-looking bug that inflates every backtest that follows.
"""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import pymupdf
import pytest

from stockanalysis.db.database import Database
from stockanalysis.extract.claude import ExtractionJob, ExtractionResult, Usage
from stockanalysis.extract.pipeline import (
    FilingRow,
    extract_one,
    pending_filings,
    persist,
)
from stockanalysis.extract.review import pending, resolve
from stockanalysis.extract.schema import AnnualReportExtraction
from stockanalysis.extract.validate import validate

ISIN = "INE000000001"
FISCAL_YEAR = 2024
PERIOD_END = dt.date(2024, 3, 31)
# When it actually became public — the statutory AGM deadline, six months later.
BROADCAST = dt.date(2024, 9, 30)


def _as_date(value) -> dt.date:
    """DuckDB hands back pandas Timestamps for DATE columns."""
    return pd.Timestamp(value).date()


def statements_pdf() -> bytes:
    rows = "\n".join(
        f"Line item {i} {i * 1234:,}.00 {i * 987:,}.00" for i in range(24)
    )
    pages = [
        "Consolidated Balance Sheet as at 31 March 2024\n(Rs. in crore)\n" + rows,
        "Consolidated Statement of Profit and Loss\n" + rows,
        "Consolidated Statement of Cash Flows\n" + rows,
        "Notes to the Consolidated Financial Statements\n" + rows,
    ]
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 780), text, fontsize=8)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def good_payload(**overrides) -> AnnualReportExtraction:
    base = dict(
        period_end_date=PERIOD_END,
        basis="CONSOLIDATED",
        reporting_unit="CRORE",
        currency="INR",
        revenue=1000.0,
        other_income=50.0,
        total_income=1050.0,
        total_expenses=850.0,
        profit_before_tax=200.0,
        tax_expense=50.0,
        pat=150.0,
        eps_basic=15.0,
        total_assets=5000.0,
        total_equity=2000.0,
        total_liabilities=3000.0,
        total_debt=1200.0,
        cash=300.0,
        ocf=180.0,
        capex=80.0,
        auditor_opinion="UNMODIFIED",
    )
    base.update(overrides)
    return AnnualReportExtraction(**base)


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def filing(db: Database, tmp_path) -> FilingRow:
    """One company with one annual report registered and on disk."""
    db.upsert_df(
        "instruments",
        pd.DataFrame(
            [
                {
                    "isin": ISIN,
                    "nse_symbol": "TESTCO",
                    "bse_code": None,
                    "name": "Test Company Limited",
                    "sector": "IT",
                    "industry": "IT",
                    "listing_date": dt.date(2010, 1, 1),
                    "delisting_date": None,
                    "is_active": True,
                }
            ]
        ),
        ["isin"],
    )

    pdf_path = tmp_path / f"{FISCAL_YEAR}.pdf"
    pdf_path.write_bytes(statements_pdf())

    filing_id = f"{ISIN}-{FISCAL_YEAR}-AR"
    db.upsert_df(
        "filings",
        pd.DataFrame(
            [
                {
                    "filing_id": filing_id,
                    "isin": ISIN,
                    "doc_type": "ANNUAL_REPORT",
                    "fiscal_year": FISCAL_YEAR,
                    "period_end": PERIOD_END,
                    "broadcast_date": BROADCAST,
                    "broadcast_date_source": "ASSUMED_AGM_DEADLINE",
                    "source_url": "https://example.invalid/ar.pdf",
                    "local_path": str(pdf_path),
                    "sha256": "0" * 64,
                    "page_count": 4,
                    "bytes": pdf_path.stat().st_size,
                }
            ]
        ),
        ["filing_id"],
    )

    return FilingRow(
        filing_id=filing_id,
        isin=ISIN,
        symbol="TESTCO",
        company="Test Company Limited",
        fiscal_year=FISCAL_YEAR,
        period_end=PERIOD_END,
        broadcast_date=BROADCAST,
        broadcast_date_source="ASSUMED_AGM_DEADLINE",
        local_path=str(pdf_path),
    )


class FakeExtractor:
    """Returns a canned extraction. Keeps these tests offline and free."""

    def __init__(self, payload=None, error=None, model="fake-model"):
        self.model = model
        self.payload = payload
        self.error = error
        self.calls = 0

    def extract(self, job: ExtractionJob) -> ExtractionResult:
        self.calls += 1
        return ExtractionResult(
            job=job,
            model=self.model,
            mode="SYNC",
            payload=self.payload,
            error=self.error,
            usage=Usage(input_tokens=120_000, output_tokens=800, cache_read_tokens=1500),
            latency_seconds=12.0,
        )


def _persist(db, filing, payload, model="fake-model"):
    result = ExtractionResult(
        job=ExtractionJob(
            filing_id=filing.filing_id, isin=filing.isin, symbol=filing.symbol,
            company=filing.company, fiscal_year=filing.fiscal_year, pdf_bytes=b"",
        ),
        model=model,
        mode="SYNC",
        payload=payload,
    )
    report = validate(payload, fiscal_year=filing.fiscal_year)
    return persist(db, filing, result, report), report


# ----------------------------------------------------------------------
# The point-in-time contract
# ----------------------------------------------------------------------


def test_filing_date_is_the_broadcast_date_not_the_period_end(db, filing):
    """The extraction reports a period ending 31 March 2024. The row's knowledge
    date must still be the September broadcast date."""
    _persist(db, filing, good_payload())

    row = db.query("SELECT * FROM fundamentals_annual").iloc[0]
    assert _as_date(row["filing_date"]) == BROADCAST
    assert _as_date(row["period_end_date"]) == PERIOD_END
    assert row["filing_date"] > row["period_end_date"]


def test_extraction_cannot_override_the_knowledge_date(db, filing):
    """Even when the model reports a period end, a signing date, or anything
    else date-shaped, filing_date comes from `filings`. The model has no route
    to this column."""
    _persist(db, filing, good_payload(period_end_date=dt.date(2024, 4, 15)))
    row = db.query("SELECT * FROM fundamentals_annual").iloc[0]
    assert _as_date(row["filing_date"]) == BROADCAST


def test_backtest_cannot_see_a_filing_before_it_was_published(db, filing):
    """The phase-0 read path, applied to phase-1 data.

    A decision made in June 2024 must not see figures published in September,
    even though the period they describe closed in March.
    """
    _persist(db, filing, good_payload())

    before = db.as_of_fundamentals([ISIN], dt.date(2024, 6, 30))
    assert before.empty, "FY2024 figures were visible four months before publication"

    on_the_day = db.as_of_fundamentals([ISIN], BROADCAST)
    assert len(on_the_day) == 1

    after = db.as_of_fundamentals([ISIN], dt.date(2024, 12, 31))
    assert len(after) == 1
    assert after.iloc[0]["revenue"] == pytest.approx(1000.0)


def test_fiscal_year_comes_from_the_filing_not_the_extraction(db, filing):
    """A model that misreads the year should surface as a validator failure,
    not silently land in a different year's row."""
    _persist(db, filing, good_payload(period_end_date=dt.date(2022, 3, 31)))
    row = db.query("SELECT * FROM fundamentals_annual").iloc[0]
    assert row["fiscal_year"] == FISCAL_YEAR


# ----------------------------------------------------------------------
# Units
# ----------------------------------------------------------------------


def test_amounts_are_stored_in_crore_regardless_of_reported_unit(db, filing):
    payload = good_payload(
        reporting_unit="LAKH",
        revenue=100_000.0,       # 1000 crore
        other_income=5_000.0,
        total_income=105_000.0,
        total_expenses=85_000.0,
        profit_before_tax=20_000.0,
        tax_expense=5_000.0,
        pat=15_000.0,
        total_assets=500_000.0,
        total_equity=200_000.0,
        total_liabilities=300_000.0,
        total_debt=120_000.0,
        cash=30_000.0,
        ocf=18_000.0,
        capex=8_000.0,
        eps_basic=15.0,
    )
    _persist(db, filing, payload)

    row = db.query("SELECT * FROM fundamentals_annual").iloc[0]
    assert row["revenue"] == pytest.approx(1000.0)
    assert row["total_assets"] == pytest.approx(5000.0)
    assert row["eps"] == pytest.approx(15.0)  # per share, never scaled


def test_fcf_is_derived_when_the_report_does_not_state_it(db, filing):
    _persist(db, filing, good_payload(fcf=None))
    row = db.query("SELECT * FROM fundamentals_annual").iloc[0]
    assert row["fcf"] == pytest.approx(100.0)  # ocf 180 - capex 80


# ----------------------------------------------------------------------
# Confidence routing
# ----------------------------------------------------------------------


def test_clean_extraction_persists_and_is_not_queued(db, filing):
    _, report = _persist(db, filing, good_payload())
    assert report.confidence == 1.0
    assert len(db.query("SELECT * FROM fundamentals_annual")) == 1
    assert pending(db).empty


def test_flagged_extraction_persists_but_is_also_queued(db, filing):
    """confidence 0.6 is usable-but-worth-a-look, per DESIGN. Dropping it
    outright would delete companies with unusual reporting formats."""
    _, report = _persist(db, filing, good_payload(capex=-80.0))
    assert report.confidence == 0.6
    assert len(db.query("SELECT * FROM fundamentals_annual")) == 1
    assert len(pending(db)) == 1


def test_consolidated_group_with_minorities_reaches_fundamentals(db, filing):
    """The RELIANCE case. PBT - tax overshoots `pat` by the minority's share of
    profit, which is correct consolidated accounting rather than a misreading,
    and the row has to persist with both legs of the split stored alongside it.
    """
    _, report = _persist(
        db,
        filing,
        good_payload(
            profit_before_tax=200.0,
            tax_expense=50.0,
            share_of_associates=5.0,
            non_controlling_interest=20.0,
            pat=135.0,
        ),
    )
    assert report.confidence == 1.0

    row = db.query("SELECT * FROM fundamentals_annual").iloc[0]
    assert row["pat"] == pytest.approx(135.0)
    assert row["non_controlling_interest"] == pytest.approx(20.0)
    assert row["share_of_associates"] == pytest.approx(5.0)


def test_hard_failure_is_queued_and_never_reaches_fundamentals(db, filing):
    _, report = _persist(db, filing, good_payload(total_assets=4000.0))
    assert report.confidence == 0.0
    assert db.query("SELECT * FROM fundamentals_annual").empty
    assert len(pending(db)) == 1


def test_every_attempt_is_recorded_even_when_it_fails(db, filing):
    _persist(db, filing, good_payload(total_assets=4000.0))
    attempts = db.query("SELECT * FROM extraction_attempts")
    assert len(attempts) == 1
    assert attempts.iloc[0]["confidence"] == 0.0
    # The raw payload survives so "what did the model actually say" stays
    # answerable months later.
    assert attempts.iloc[0]["payload_json"]
    assert attempts.iloc[0]["checks_json"]


def test_api_failure_is_recorded_and_queued(db, filing):
    result = ExtractionResult(
        job=ExtractionJob(
            filing_id=filing.filing_id, isin=ISIN, symbol="TESTCO",
            company="Test Company Limited", fiscal_year=FISCAL_YEAR, pdf_bytes=b"",
        ),
        model="fake-model",
        mode="SYNC",
        error="APIStatusError: 529 overloaded",
    )
    persist(db, filing, result, None)

    assert db.query("SELECT * FROM fundamentals_annual").empty
    assert db.query("SELECT * FROM extraction_attempts").iloc[0]["error"]
    assert len(pending(db)) == 1


# ----------------------------------------------------------------------
# End-to-end through the locator, with a fake model
# ----------------------------------------------------------------------


def test_extract_one_runs_the_locator_and_persists(db, filing):
    extractor = FakeExtractor(payload=good_payload())
    result, report = extract_one(db, filing, extractor)

    assert extractor.calls == 1
    assert result.ok and report.confidence == 1.0
    # The locator ran: only the statement pages were sent, and its choice is
    # recorded so a bad extraction can be traced to the pages it saw.
    attempt = db.query("SELECT * FROM extraction_attempts").iloc[0]
    assert attempt["pages_sent"] > 0
    assert attempt["source_pages"]


def test_missing_pdf_is_an_error_not_a_crash(db, filing):
    filing.local_path = "/nonexistent/report.pdf"
    result, report = extract_one(db, filing, FakeExtractor(payload=good_payload()))

    assert report is None
    assert "FileNotFoundError" in result.error
    assert db.query("SELECT * FROM fundamentals_annual").empty
    assert len(pending(db)) == 1


def test_pending_filings_skips_already_extracted(db, filing):
    assert len(pending_filings(db)) == 1
    extract_one(db, filing, FakeExtractor(payload=good_payload()))
    assert pending_filings(db) == []
    assert len(pending_filings(db, only_unextracted=False)) == 1


def test_failed_attempts_do_not_count_as_extracted(db, filing):
    """Retrying a transient 529 must not require deleting rows by hand."""
    extract_one(db, filing, FakeExtractor(error="APIStatusError: 529 overloaded"))
    assert len(pending_filings(db)) == 1


# ----------------------------------------------------------------------
# Review queue
# ----------------------------------------------------------------------


def test_rejecting_pulls_the_row_back_out_of_fundamentals(db, filing):
    attempt_id, _ = _persist(db, filing, good_payload(capex=-80.0))
    assert len(db.query("SELECT * FROM fundamentals_annual")) == 1

    resolve(db, attempt_id, "REJECTED", notes="read the wrong column")

    assert db.query("SELECT * FROM fundamentals_annual").empty
    assert pending(db).empty


def test_accepting_leaves_the_row_in_place(db, filing):
    attempt_id, _ = _persist(db, filing, good_payload(capex=-80.0))
    resolve(db, attempt_id, "ACCEPTED", notes="capex genuinely shown as negative")

    assert len(db.query("SELECT * FROM fundamentals_annual")) == 1
    assert pending(db).empty


def test_accepting_with_persist_overrides_a_below_threshold_row(db, filing):
    """A human can overrule a validator that fired on a legitimately unusual
    report — a bank with no capex line, say — but the row keeps its original
    confidence so the override stays visible downstream."""
    attempt_id, report = _persist(db, filing, good_payload(total_assets=4000.0))
    assert report.confidence == 0.0
    assert db.query("SELECT * FROM fundamentals_annual").empty

    resolve(db, attempt_id, "ACCEPTED", notes="verified by hand", force_persist=True)

    rows = db.query("SELECT * FROM fundamentals_annual")
    assert len(rows) == 1
    assert rows.iloc[0]["extraction_confidence"] == 0.0
    assert _as_date(rows.iloc[0]["filing_date"]) == BROADCAST


def test_resolve_rejects_an_unknown_status(db, filing):
    attempt_id, _ = _persist(db, filing, good_payload(capex=-80.0))
    with pytest.raises(ValueError, match="ACCEPTED or REJECTED"):
        resolve(db, attempt_id, "MAYBE")
