"""Arithmetic validation and confidence scoring.

The model reads; this module checks the reading. Every check here is an identity
that must hold in any correctly-extracted set of statements, so a failure means
the extraction is wrong — not that the company is unusual.

Checks are graded:

  HARD  an accounting identity, or a field the factor model cannot work without.
        A failure means the row does not get persisted at all.
  SOFT  a consistency check that a legitimately odd report can fail — a bank with
        no capex line, a company whose quarterly filings are standalone while its
        annual report is consolidated. Failures reduce confidence and flag the
        row for review; they do not discard it.

The distinction is the difference between a pipeline that quietly drops a third
of the banking sector and one that flags it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from stockanalysis.extract.schema import AnnualReportExtraction, to_crore

# Tolerances. Balance-sheet arithmetic is exact in principle; the slack is for
# rounding in the printed statements, which are themselves rounded to the
# reporting unit.
BALANCE_SHEET_TOL = 0.005  # 0.5%
PROFIT_TOL = 0.02  # 2%
FCF_TOL = 0.01  # 1%
NSE_REVENUE_TOL = 0.10  # 10%
NSE_PAT_TOL = 0.15  # 15%

# Without these, no factor in the model can be computed for this company-year.
REQUIRED_FIELDS = ("revenue", "pat", "total_assets", "total_equity", "ocf")


@dataclass
class Check:
    name: str
    passed: bool
    severity: str  # HARD | SOFT
    detail: str
    skipped: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "detail": self.detail,
            "skipped": self.skipped,
        }


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.skipped and not c.passed and c.severity == "HARD"]

    @property
    def soft_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.skipped and not c.passed and c.severity == "SOFT"]

    @property
    def confidence(self) -> float:
        """0.0 unusable, 0.3 doubtful, 0.6 flagged, 1.0 clean.

        Deliberately coarse. A finer scale would imply a precision these checks
        do not have, and would invite someone to pick a threshold like 0.72 that
        means nothing.
        """
        if self.hard_failures:
            return 0.0
        n_soft = len(self.soft_failures)
        if n_soft == 0:
            return 1.0
        if n_soft == 1:
            return 0.6
        return 0.3

    @property
    def reasons(self) -> str:
        failed = self.hard_failures + self.soft_failures
        return "; ".join(f"{c.name}: {c.detail}" for c in failed)

    def as_dict(self) -> dict:
        return {
            "confidence": self.confidence,
            "checks": [c.as_dict() for c in self.checks],
        }


def _rel_diff(a: float, b: float) -> float:
    """Relative difference against the larger magnitude.

    Dividing by `a` alone blows up when the denominator is near zero — which is
    exactly the case for a company at breakeven, where PAT is tiny and every
    profit identity would otherwise look catastrophically violated.
    """
    scale = max(abs(a), abs(b))
    if scale == 0:
        return 0.0
    return abs(a - b) / scale


def validate(
    payload: AnnualReportExtraction,
    fiscal_year: int | None = None,
    nse_quarterly: list[dict] | None = None,
) -> ValidationReport:
    """Run every check against a single extraction.

    `nse_quarterly` is the free ground truth from `NSE.results_comparison`:
    rows with `period_end`, `revenue` and `pat` already converted to crore. When
    four quarters of a fiscal year are available, their sum should approximate
    the annual figures.
    """
    report = ValidationReport()
    add = report.checks.append

    # ---- units must be known before anything else can be checked ----
    try:
        v = to_crore(payload)
    except ValueError as e:
        add(Check("units", False, "HARD", str(e)))
        return report
    add(Check("units", True, "HARD", f"reporting_unit={payload.reporting_unit}"))

    # ---- required fields ----
    missing = [f for f in REQUIRED_FIELDS if v.get(f) is None]
    add(
        Check(
            "required_fields",
            not missing,
            "HARD",
            f"missing {missing}" if missing else "all present",
        )
    )

    # ---- basis must be stated: a row of unknown basis cannot be compared ----
    add(
        Check(
            "basis_stated",
            payload.basis is not None,
            "HARD",
            f"basis={payload.basis}",
        )
    )

    # ---- balance sheet identity: assets == equity + liabilities ----
    assets = v.get("total_assets")
    equity = v.get("total_equity")
    liabilities = v.get("total_liabilities")
    if assets is None or equity is None or liabilities is None:
        add(
            Check(
                "balance_sheet_identity",
                False,
                "HARD" if assets is None or equity is None else "SOFT",
                "cannot check: "
                f"assets={assets}, equity={equity}, liabilities={liabilities}",
                skipped=liabilities is None and assets is not None and equity is not None,
            )
        )
        # A missing total_liabilities is a reporting-format quirk, not a wrong
        # number, so it is skipped rather than failed.
    else:
        diff = _rel_diff(assets, equity + liabilities)
        add(
            Check(
                "balance_sheet_identity",
                diff <= BALANCE_SHEET_TOL,
                "HARD",
                f"assets {assets:,.1f} vs equity+liabilities "
                f"{equity + liabilities:,.1f} ({diff:.2%})",
            )
        )

    # ---- PBT - tax == PAT ----
    pbt, tax, pat = v.get("profit_before_tax"), v.get("tax_expense"), v.get("pat")
    if pbt is None or tax is None or pat is None:
        add(
            Check(
                "pbt_tax_pat",
                True,
                "HARD",
                f"skipped: pbt={pbt}, tax={tax}, pat={pat}",
                skipped=True,
            )
        )
    else:
        diff = _rel_diff(pbt - tax, pat)
        add(
            Check(
                "pbt_tax_pat",
                diff <= PROFIT_TOL,
                "HARD",
                f"pbt-tax {pbt - tax:,.1f} vs pat {pat:,.1f} ({diff:.2%})",
            )
        )

    # ---- total income - total expenses == PBT ----
    income, expenses = v.get("total_income"), v.get("total_expenses")
    if income is None or expenses is None or pbt is None:
        add(
            Check(
                "income_expenses_pbt",
                True,
                "SOFT",
                f"skipped: income={income}, expenses={expenses}, pbt={pbt}",
                skipped=True,
            )
        )
    else:
        diff = _rel_diff(income - expenses, pbt)
        add(
            Check(
                "income_expenses_pbt",
                diff <= PROFIT_TOL,
                "SOFT",
                f"income-expenses {income - expenses:,.1f} vs pbt {pbt:,.1f} ({diff:.2%})",
            )
        )

    # ---- revenue <= total income ----
    revenue = v.get("revenue")
    if revenue is None or income is None:
        add(Check("revenue_vs_total_income", True, "SOFT", "skipped", skipped=True))
    else:
        # Other income is non-negative, so total income cannot be below revenue
        # beyond rounding. A failure usually means the two were swapped.
        add(
            Check(
                "revenue_vs_total_income",
                revenue <= income * (1 + PROFIT_TOL),
                "SOFT",
                f"revenue {revenue:,.1f} vs total income {income:,.1f}",
            )
        )

    # ---- FCF == OCF - capex, where the report states FCF ----
    ocf, capex, fcf = v.get("ocf"), v.get("capex"), v.get("fcf")
    if fcf is None or ocf is None or capex is None:
        add(
            Check(
                "fcf_identity",
                True,
                "SOFT",
                "skipped: fcf not reported (it will be derived as ocf - capex)",
                skipped=True,
            )
        )
    else:
        diff = _rel_diff(ocf - capex, fcf)
        add(
            Check(
                "fcf_identity",
                diff <= FCF_TOL,
                "SOFT",
                f"ocf-capex {ocf - capex:,.1f} vs reported fcf {fcf:,.1f} ({diff:.2%})",
            )
        )

    # ---- capex reported as a positive magnitude ----
    if capex is None:
        add(Check("capex_sign", True, "SOFT", "skipped", skipped=True))
    else:
        add(
            Check(
                "capex_sign",
                capex >= 0,
                "SOFT",
                f"capex {capex:,.1f} should be a positive magnitude",
            )
        )

    # ---- EPS sign agrees with PAT ----
    eps = v.get("eps_basic")
    if eps is None or pat is None or eps == 0 or pat == 0:
        add(Check("eps_sign", True, "SOFT", "skipped", skipped=True))
    else:
        add(
            Check(
                "eps_sign",
                (eps > 0) == (pat > 0),
                "SOFT",
                f"eps {eps} and pat {pat:,.1f} disagree in sign",
            )
        )

    # ---- period end consistent with the fiscal year we asked for ----
    if fiscal_year is None or payload.period_end_date is None:
        add(Check("period_end_matches_fy", True, "SOFT", "skipped", skipped=True))
    else:
        expected = dt.date(fiscal_year, 3, 31)
        delta = abs((payload.period_end_date - expected).days)
        add(
            Check(
                "period_end_matches_fy",
                delta <= 92,  # allows a December or June year-end company
                "SOFT",
                f"period_end {payload.period_end_date} vs expected FY{fiscal_year} "
                f"end {expected} ({delta}d)",
            )
        )

    # ---- cross-check against NSE quarterly ----
    report.checks.extend(_nse_checks(v, nse_quarterly))

    return report


def _nse_checks(v: dict, nse_quarterly: list[dict] | None) -> list[Check]:
    """Compare the annual figures to the sum of NSE's four quarterly filings.

    This is the only check that uses evidence from outside the PDF, which makes
    it the most valuable one — arithmetic identities catch internally
    inconsistent extractions, but only an external source catches an extraction
    that is internally perfect and simply read the wrong column.

    It is SOFT because NSE's `results_comparison` does not say whether it is
    reporting standalone or consolidated figures, and for a group with large
    subsidiaries the two differ far more than any tolerance we would set.
    """
    if not nse_quarterly:
        return [Check("nse_cross_check", True, "SOFT", "no quarterly data", skipped=True)]

    if len(nse_quarterly) < 4:
        return [
            Check(
                "nse_cross_check",
                True,
                "SOFT",
                f"only {len(nse_quarterly)} quarters available, need 4",
                skipped=True,
            )
        ]

    checks: list[Check] = []
    quarters = nse_quarterly[:4]

    for field_name, tol in (("revenue", NSE_REVENUE_TOL), ("pat", NSE_PAT_TOL)):
        annual = v.get(field_name)
        parts = [q.get(field_name) for q in quarters]
        if annual is None or any(p is None for p in parts):
            checks.append(
                Check(
                    f"nse_cross_check_{field_name}",
                    True,
                    "SOFT",
                    "skipped: incomplete data",
                    skipped=True,
                )
            )
            continue
        summed = sum(parts)
        diff = _rel_diff(annual, summed)
        checks.append(
            Check(
                f"nse_cross_check_{field_name}",
                diff <= tol,
                "SOFT",
                f"annual {annual:,.1f} vs 4 NSE quarters {summed:,.1f} ({diff:.2%})",
            )
        )

    return checks


def derived_fcf(payload: AnnualReportExtraction) -> float | None:
    """FCF in crore: as reported, else OCF - capex, else None.

    Kept out of `validate` so the validator never sees a derived figure — a
    derived value satisfies its own identity by construction, which would turn
    the FCF check into a tautology.
    """
    v = to_crore(payload)
    if v.get("fcf") is not None:
        return v["fcf"]
    if v.get("ocf") is not None and v.get("capex") is not None:
        return v["ocf"] - v["capex"]
    return None
