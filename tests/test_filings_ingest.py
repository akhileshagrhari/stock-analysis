"""Parsing NSE's filing and quarterly-results payloads.

Both upstream response shapes are undocumented and have changed field names
between versions of NseIndiaApi. These tests pin the parsing — the part that
breaks — without needing a live NSE session, which also means they do not
contribute to the request budget that makes getting IP-blocked the main
operational risk of this phase.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stockanalysis.ingest.filings import (
    BROADCAST_ASSUMED,
    BROADCAST_FROM_NSE,
    assumed_broadcast_date,
    parse_annual_reports,
)
from stockanalysis.ingest.nse_fundamentals import (
    LAKH_TO_CRORE,
    parse_results_comparison,
    quarterly_filing_date,
)

ISIN = "INE000000001"


def test_agm_deadline_is_six_months_after_the_year_end():
    assert assumed_broadcast_date(dt.date(2024, 3, 31)) == dt.date(2024, 9, 30)
    assert assumed_broadcast_date(dt.date(2023, 12, 31)) == dt.date(2024, 6, 30)


def test_annual_reports_are_parsed_into_refs():
    raw = {
        "data": [
            {
                "fromYr": "2023",
                "toYr": "2024",
                "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_TESTCO_2023_2024.zip",
            },
            {
                "fromYr": "2022",
                "toYr": "2023",
                "fileName": "https://nsearchives.nseindia.com/annual_reports/AR_TESTCO_2022_2023.zip",
            },
        ]
    }
    refs = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)

    assert [r.fiscal_year for r in refs] == [2024, 2023]  # newest first
    assert refs[0].period_end == dt.date(2024, 3, 31)
    assert refs[0].filing_id == f"{ISIN}-2024-AR"


def test_missing_broadcast_date_falls_back_to_the_agm_deadline():
    """NSE's listing does not reliably carry a broadcast timestamp. Defaulting
    to the period end would hand a backtest FY2024 figures in April 2024."""
    raw = {"data": [{"toYr": "2024", "fileName": "https://x.invalid/AR_2024.pdf"}]}
    ref = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)[0]

    assert ref.broadcast_date == dt.date(2024, 9, 30)
    assert ref.broadcast_date_source == BROADCAST_ASSUMED
    assert ref.broadcast_date > ref.period_end


def test_a_real_broadcast_date_is_used_and_labelled():
    raw = {
        "data": [
            {
                "toYr": "2024",
                "fileName": "https://x.invalid/AR_2024.pdf",
                "submissionDate": "12-Aug-2024",
            }
        ]
    }
    ref = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)[0]

    assert ref.broadcast_date == dt.date(2024, 8, 12)
    assert ref.broadcast_date_source == BROADCAST_FROM_NSE


def test_an_impossible_broadcast_date_is_rejected_not_trusted():
    """A date on or before the period end cannot be when the report was
    published. Trusting it would manufacture lookahead."""
    raw = {
        "data": [
            {
                "toYr": "2024",
                "fileName": "https://x.invalid/AR_2024.pdf",
                "submissionDate": "01-Jan-2024",
            }
        ]
    }
    ref = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)[0]

    assert ref.broadcast_date_source == BROADCAST_ASSUMED
    assert ref.broadcast_date == dt.date(2024, 9, 30)


def test_fiscal_year_falls_back_to_the_filename():
    raw = {
        "data": [
            {"fileName": "https://x.invalid/annual_reports/AR_ULTRACEMCO_2010_2011_0808.zip"}
        ]
    }
    refs = parse_annual_reports(raw, symbol="ULTRACEMCO", isin=ISIN)
    assert refs and refs[0].fiscal_year == 2011


def test_alternative_field_names_are_tolerated():
    raw = {"reports": [{"to_yr": "2024", "attchmntFile": "https://x.invalid/a.pdf"}]}
    refs = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)
    assert refs and refs[0].fiscal_year == 2024


def test_unusable_records_are_skipped_not_fatal():
    raw = {
        "data": [
            {"toYr": "2024", "fileName": "not-a-url"},
            {"fileName": "https://x.invalid/no-year-anywhere.pdf"},
            {"toYr": "2023", "fileName": "https://x.invalid/AR.pdf"},
        ]
    }
    refs = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)
    assert [r.fiscal_year for r in refs] == [2023]


def test_duplicate_years_prefer_the_one_with_a_real_date():
    raw = {
        "data": [
            {"toYr": "2024", "fileName": "https://x.invalid/a.pdf"},
            {"toYr": "2024", "fileName": "https://x.invalid/b.pdf",
             "submissionDate": "12-Aug-2024"},
        ]
    }
    refs = parse_annual_reports(raw, symbol="TESTCO", isin=ISIN)
    assert len(refs) == 1
    assert refs[0].broadcast_date_source == BROADCAST_FROM_NSE


def test_empty_response_yields_nothing():
    assert parse_annual_reports({}, symbol="TESTCO", isin=ISIN) == []
    assert parse_annual_reports(None, symbol="TESTCO", isin=ISIN) == []


# ----------------------------------------------------------------------
# Quarterly results
# ----------------------------------------------------------------------


def test_quarterly_amounts_convert_from_lakhs_to_crore():
    """NSE reports in lakhs; everything else here is in crore. Getting this
    wrong is a silent 100x error in every valuation factor."""
    raw = {
        "resCmpData": [
            {"re_to_dt": "31-Mar-2024", "re_total_inc": "250000", "re_net_profit": "37500",
             "re_basic_eps": "12.5"},
        ]
    }
    q = parse_results_comparison(raw, ISIN)[0]

    assert q.revenue == pytest.approx(2500.0)  # 250,000 lakh == 2,500 crore
    assert q.pat == pytest.approx(375.0)
    assert LAKH_TO_CRORE == 0.01


def test_quarterly_eps_is_not_scaled():
    raw = {"resCmpData": [{"re_to_dt": "31-Mar-2024", "re_basic_eps": "12.5"}]}
    assert parse_results_comparison(raw, ISIN)[0].eps == pytest.approx(12.5)


def test_quarterly_filing_date_allows_the_reporting_window():
    """SEBI LODR gives 45 days after quarter end. Using the period end itself
    would let a backtest read results before they were filed."""
    assert quarterly_filing_date(dt.date(2024, 3, 31)) == dt.date(2024, 5, 15)


def test_quarterly_rows_come_back_newest_first():
    raw = {
        "resCmpData": [
            {"re_to_dt": "30-Jun-2023", "re_total_inc": "100"},
            {"re_to_dt": "31-Mar-2024", "re_total_inc": "400"},
            {"re_to_dt": "31-Dec-2023", "re_total_inc": "300"},
        ]
    }
    quarters = parse_results_comparison(raw, ISIN)
    assert [q.period_end for q in quarters] == [
        dt.date(2024, 3, 31), dt.date(2023, 12, 31), dt.date(2023, 6, 30),
    ]


def test_quarterly_handles_commas_and_blanks():
    raw = {
        "resCmpData": [
            {"re_to_dt": "31-Mar-2024", "re_total_inc": "1,25,000", "re_net_profit": "-"},
        ]
    }
    q = parse_results_comparison(raw, ISIN)[0]
    assert q.revenue == pytest.approx(1250.0)
    assert q.pat is None


def test_quarterly_rows_without_a_period_end_are_dropped():
    raw = {"resCmpData": [{"re_total_inc": "1000"}, {"re_to_dt": "31-Mar-2024"}]}
    assert len(parse_results_comparison(raw, ISIN)) == 1
