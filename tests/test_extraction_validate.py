"""Validator behaviour and confidence scoring.

The validators are the only thing standing between a confident misreading and a
factor model built on it, so their failure modes matter as much as their
successes: a check that fires on every bank is worse than no check at all,
because it trains whoever reads the review queue to ignore it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stockanalysis.extract.schema import AnnualReportExtraction
from stockanalysis.extract.validate import derived_fcf, validate


def clean_payload(**overrides) -> AnnualReportExtraction:
    """An internally consistent extraction. Every identity holds exactly."""
    base = dict(
        fiscal_year_label="2023-24",
        period_end_date=dt.date(2024, 3, 31),
        basis="CONSOLIDATED",
        reporting_unit="CRORE",
        currency="INR",
        revenue=1000.0,
        other_income=50.0,
        total_income=1050.0,
        total_expenses=850.0,
        interest_expense=30.0,
        depreciation=60.0,
        profit_before_tax=200.0,
        tax_expense=50.0,
        pat=150.0,
        eps_basic=15.0,
        total_assets=5000.0,
        total_equity=2000.0,
        total_liabilities=3000.0,
        total_debt=1200.0,
        cash=300.0,
        contingent_liabilities=400.0,
        ocf=180.0,
        capex=80.0,
        auditor_opinion="UNMODIFIED",
    )
    base.update(overrides)
    return AnnualReportExtraction(**base)


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_clean_extraction_scores_one():
    report = validate(clean_payload(), fiscal_year=2024)
    assert report.confidence == 1.0
    assert not report.hard_failures and not report.soft_failures


def test_broken_balance_sheet_is_a_hard_failure():
    """assets != equity + liabilities means the extraction is wrong, full stop."""
    report = validate(clean_payload(total_assets=4000.0), fiscal_year=2024)
    assert report.confidence == 0.0
    assert _check(report, "balance_sheet_identity").passed is False


def test_balance_sheet_tolerates_printed_rounding():
    # 0.2% off — within what rounding to the reporting unit can produce.
    report = validate(clean_payload(total_assets=5010.0), fiscal_year=2024)
    assert _check(report, "balance_sheet_identity").passed


def test_pbt_minus_tax_must_equal_pat():
    report = validate(clean_payload(pat=90.0), fiscal_year=2024)
    assert report.confidence == 0.0
    assert _check(report, "pbt_tax_pat").passed is False


def test_minority_interest_completes_the_profit_identity():
    """A consolidated group's PBT - tax is the *whole* group's profit; `pat` is
    only the parent's share of it. RELIANCE FY2025, as printed: the 11,139 crore
    the naive identity treats as an extraction error is the minority's profit
    plus the associates' contribution, and both are stated on the same page.
    """
    report = validate(
        clean_payload(
            profit_before_tax=106017.0,
            tax_expense=25230.0,
            share_of_associates=522.0,
            non_controlling_interest=11661.0,
            pat=69648.0,
            # keep the unrelated identities satisfied
            revenue=980136.0, other_income=17978.0, total_income=998114.0,
            total_expenses=892097.0, eps_basic=51.47,
            total_assets=1950121.0, total_equity=1009626.0,
            total_liabilities=940495.0,
        ),
        fiscal_year=2025,
    )
    assert _check(report, "pbt_tax_pat").passed
    assert not report.hard_failures


def test_minority_interest_does_not_excuse_a_wrong_pat():
    """The identity still has to close. Reporting an NCI does not turn the check
    off — otherwise it would stop catching the misread column it exists for."""
    report = validate(
        clean_payload(non_controlling_interest=10.0, pat=90.0), fiscal_year=2024
    )
    assert _check(report, "pbt_tax_pat").passed is False
    assert report.confidence == 0.0


def test_absent_minority_interest_is_treated_as_zero():
    """Most companies have no subsidiaries with outside shareholders and print
    no such line. A null must not be read as "unknown, skip the check"."""
    report = validate(clean_payload(pat=90.0, non_controlling_interest=None),
                      fiscal_year=2024)
    assert _check(report, "pbt_tax_pat").passed is False


def test_missing_required_field_is_a_hard_failure():
    report = validate(clean_payload(ocf=None), fiscal_year=2024)
    assert report.confidence == 0.0
    assert "ocf" in _check(report, "required_fields").detail


def test_missing_basis_is_a_hard_failure():
    """A row of unknown basis cannot be compared to anything, and the PK
    treats basis as part of the identity."""
    report = validate(clean_payload(basis=None), fiscal_year=2024)
    assert report.confidence == 0.0


def test_one_soft_failure_scores_point_six():
    report = validate(clean_payload(capex=-80.0), fiscal_year=2024)
    assert report.confidence == 0.6
    assert _check(report, "capex_sign").passed is False


def test_two_soft_failures_score_point_three():
    report = validate(
        clean_payload(capex=-80.0, eps_basic=-15.0), fiscal_year=2024
    )
    assert len(report.soft_failures) == 2
    assert report.confidence == 0.3


def test_missing_optional_fields_are_skipped_not_failed():
    """A bank has no meaningful capex line. Skipping is what keeps the review
    queue about real errors instead of about the banking sector."""
    report = validate(
        clean_payload(capex=None, fcf=None, other_income=None, total_income=None),
        fiscal_year=2024,
    )
    assert report.confidence == 1.0
    assert _check(report, "capex_sign").skipped
    assert _check(report, "income_expenses_pbt").skipped


def test_missing_total_liabilities_is_skipped_not_a_hard_failure():
    """Some formats do not print a Total Liabilities line. That is a reporting
    quirk, not a wrong number, so it must not discard the row."""
    report = validate(clean_payload(total_liabilities=None), fiscal_year=2024)
    assert _check(report, "balance_sheet_identity").skipped
    assert report.confidence > 0.0


def test_unknown_units_fail_immediately_and_stop():
    report = validate(clean_payload(reporting_unit=None), fiscal_year=2024)
    assert report.confidence == 0.0
    # Nothing downstream can be checked without knowing the unit, so it stops.
    assert len(report.checks) == 1


def test_near_breakeven_profit_does_not_trip_the_profit_identity():
    """Relative difference is taken against the larger magnitude. Dividing by
    PAT alone would make a company at breakeven fail every profit check."""
    report = validate(
        clean_payload(
            profit_before_tax=0.1, tax_expense=0.05, pat=0.05,
            total_income=1050.0, total_expenses=1049.9,
        ),
        fiscal_year=2024,
    )
    assert _check(report, "pbt_tax_pat").passed


def test_eps_sign_must_agree_with_pat():
    report = validate(clean_payload(eps_basic=-15.0), fiscal_year=2024)
    assert _check(report, "eps_sign").passed is False


def test_period_end_far_from_fiscal_year_is_flagged():
    report = validate(
        clean_payload(period_end_date=dt.date(2022, 3, 31)), fiscal_year=2024
    )
    assert _check(report, "period_end_matches_fy").passed is False


# ----------------------------------------------------------------------
# NSE cross-check — the only evidence from outside the PDF
# ----------------------------------------------------------------------


def quarters(revenue_each: float, pat_each: float) -> list[dict]:
    return [
        {"period_end": dt.date(2024, 3, 31), "revenue": revenue_each, "pat": pat_each},
        {"period_end": dt.date(2023, 12, 31), "revenue": revenue_each, "pat": pat_each},
        {"period_end": dt.date(2023, 9, 30), "revenue": revenue_each, "pat": pat_each},
        {"period_end": dt.date(2023, 6, 30), "revenue": revenue_each, "pat": pat_each},
    ]


def test_nse_cross_check_passes_when_quarters_sum_to_the_annual_figure():
    report = validate(
        clean_payload(), fiscal_year=2024, nse_quarterly=quarters(250.0, 37.5)
    )
    assert _check(report, "nse_cross_check_revenue").passed
    assert report.confidence == 1.0


def test_nse_cross_check_catches_a_wrong_column():
    """The failure this exists for: an internally perfect extraction that read
    the prior-year column throughout. Every arithmetic identity still holds."""
    report = validate(
        clean_payload(), fiscal_year=2024, nse_quarterly=quarters(500.0, 75.0)
    )
    assert _check(report, "nse_cross_check_revenue").passed is False
    assert report.confidence < 1.0


def test_nse_cross_check_is_skipped_without_four_quarters():
    report = validate(
        clean_payload(), fiscal_year=2024, nse_quarterly=quarters(250.0, 37.5)[:3]
    )
    assert _check(report, "nse_cross_check").skipped
    assert report.confidence == 1.0


def test_nse_cross_check_never_hard_fails():
    """results_comparison does not say whether it is standalone or consolidated.
    For a group with large subsidiaries the two differ by more than any
    tolerance, so this can only ever flag."""
    report = validate(
        clean_payload(), fiscal_year=2024, nse_quarterly=quarters(50.0, 5.0)
    )
    assert report.confidence > 0.0
    assert not report.hard_failures


# ----------------------------------------------------------------------
# Derived FCF
# ----------------------------------------------------------------------


def test_fcf_is_derived_when_not_reported():
    assert derived_fcf(clean_payload(fcf=None)) == pytest.approx(100.0)


def test_reported_fcf_wins_over_derivation():
    assert derived_fcf(clean_payload(fcf=95.0)) == pytest.approx(95.0)


def test_derived_fcf_never_feeds_the_validator():
    """A derived value satisfies its own identity by construction. If validate()
    saw it, the FCF check would be a tautology that always passes."""
    report = validate(clean_payload(fcf=None), fiscal_year=2024)
    assert _check(report, "fcf_identity").skipped


def test_reported_fcf_inconsistent_with_ocf_and_capex_is_flagged():
    report = validate(clean_payload(fcf=500.0), fiscal_year=2024)
    assert _check(report, "fcf_identity").passed is False
