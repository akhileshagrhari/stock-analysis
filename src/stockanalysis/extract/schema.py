"""The extraction contract.

Two rules govern this schema, and both exist because of how the API's structured
outputs work:

1. **No numeric constraints.** `minimum`, `maximum`, `multipleOf` and friends are
   not supported by the API's JSON-schema subset. Range checking belongs in
   `validate.py`, where a failure produces a confidence score and a review-queue
   entry rather than a 400.

2. **Every field is optional.** A strict schema requires all properties, so
   "absent" has to be expressible as `null`. That is also the honest encoding:
   forcing the model to emit a number for a line item the report does not contain
   is an invitation to invent one. A `None` we can detect and flag; a plausible
   fabrication we cannot.

Amounts are extracted **as printed**, together with the statement's stated unit.
Conversion happens once, deterministically, in `to_crore`. Asking the model to
do arithmetic on units it just read is the single easiest way to introduce a
100x error into every downstream factor.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

# Indian annual reports state amounts in whatever unit the board preferred that
# year. The same company changes it between years.
ReportingUnit = Literal["ABSOLUTE", "THOUSAND", "LAKH", "MILLION", "CRORE", "BILLION"]

Basis = Literal["CONSOLIDATED", "STANDALONE"]

AuditorOpinion = Literal[
    "UNMODIFIED",  # clean
    "QUALIFIED",
    "ADVERSE",
    "DISCLAIMER",
    "NOT_STATED",
]

# Multipliers into crore (10^7 rupees), the unit we store.
_TO_CRORE: dict[str, float] = {
    "ABSOLUTE": 1e-7,
    "THOUSAND": 1e-4,
    "LAKH": 1e-2,
    "MILLION": 1e-1,
    "CRORE": 1.0,
    "BILLION": 1e2,
}

# Per-share figures are already per-share. Scaling them by the statement's unit
# turns an EPS of 84.20 into 0.0000084, which then quietly poisons every
# valuation factor that divides by it.
PER_SHARE_FIELDS = frozenset({"eps_basic", "eps_diluted"})

# Fields carrying a monetary amount in `reporting_unit`.
MONETARY_FIELDS = (
    "revenue",
    "other_income",
    "total_income",
    "total_expenses",
    "ebitda",
    "depreciation",
    "interest_expense",
    "profit_before_tax",
    "tax_expense",
    "share_of_associates",
    "non_controlling_interest",
    "pat",
    "total_assets",
    "total_equity",
    "total_liabilities",
    "total_debt",
    "cash",
    "contingent_liabilities",
    "ocf",
    "capex",
    "fcf",
)


class AnnualReportExtraction(BaseModel):
    """Structured financials for one company-year, as printed in the report."""

    # ---------------- provenance ----------------
    fiscal_year_label: str | None = Field(
        None, description="Fiscal year exactly as printed, e.g. '2023-24' or 'FY2024'."
    )
    period_end_date: dt.date | None = Field(
        None, description="Closing date of the reporting period, usually 31 March."
    )
    basis: Basis | None = Field(
        None,
        description=(
            "Which set of statements these figures come from. Use CONSOLIDATED "
            "whenever the report contains consolidated statements."
        ),
    )
    reporting_unit: ReportingUnit | None = Field(
        None,
        description=(
            "The unit the statements are denominated in, from the header line "
            "such as '(Rs. in crore)' or '(Amount in Rs. lakhs)'. Applies to all "
            "monetary fields, never to per-share figures."
        ),
    )
    currency: str | None = Field(None, description="ISO currency code, normally INR.")

    # ---------------- statement of profit and loss ----------------
    revenue: float | None = Field(
        None,
        description=(
            "Revenue from Operations. NOT Total Income — Total Income adds other "
            "income and is reported separately below."
        ),
    )
    other_income: float | None = None
    total_income: float | None = Field(
        None, description="Total Income (revenue from operations plus other income)."
    )
    total_expenses: float | None = Field(None, description="Total Expenses line.")
    ebitda: float | None = Field(
        None,
        description=(
            "Only if the report states EBITDA or 'Operating Profit' explicitly. "
            "Leave null rather than deriving it."
        ),
    )
    depreciation: float | None = Field(
        None, description="Depreciation and amortisation expense."
    )
    interest_expense: float | None = Field(None, description="Finance costs.")
    profit_before_tax: float | None = None
    tax_expense: float | None = Field(
        None, description="Total tax expense: current plus deferred."
    )
    share_of_associates: float | None = Field(
        None,
        description=(
            "Share of profit or loss of associates and joint ventures, as a "
            "separate line between profit before tax and tax expense in a "
            "consolidated statement. Negative for a share of losses. Null when "
            "the statement has no such line."
        ),
    )
    non_controlling_interest: float | None = Field(
        None,
        description=(
            "The portion of profit for the year attributable to non-controlling "
            "(minority) interests, from the 'Profit attributable to' split at "
            "the foot of a consolidated statement of profit and loss. Null when "
            "the statement shows no such split."
        ),
    )
    pat: float | None = Field(
        None,
        description=(
            "Profit After Tax for the year. Where the report splits profit "
            "attributable to owners versus non-controlling interests, use profit "
            "attributable to owners of the parent, and report the other half of "
            "that split in `non_controlling_interest`."
        ),
    )
    eps_basic: float | None = Field(
        None, description="Basic earnings per share, in rupees per share."
    )
    eps_diluted: float | None = Field(None, description="Diluted EPS, rupees per share.")

    # ---------------- balance sheet ----------------
    total_assets: float | None = None
    total_equity: float | None = Field(
        None, description="Total equity, including non-controlling interests."
    )
    total_liabilities: float | None = Field(
        None, description="Total liabilities: current plus non-current."
    )
    total_debt: float | None = Field(
        None,
        description=(
            "Interest-bearing borrowings, current plus non-current. Excludes "
            "trade payables and other non-debt liabilities."
        ),
    )
    cash: float | None = Field(
        None, description="Cash and cash equivalents, plus bank balances treated as such."
    )
    contingent_liabilities: float | None = Field(
        None, description="Total contingent liabilities from the notes."
    )

    # ---------------- cash flow ----------------
    ocf: float | None = Field(
        None, description="Net cash generated from operating activities."
    )
    capex: float | None = Field(
        None,
        description=(
            "Purchase of property, plant and equipment plus intangibles, as a "
            "POSITIVE magnitude even though the cash flow statement shows it as "
            "an outflow in brackets."
        ),
    )
    fcf: float | None = Field(
        None, description="Only if the report states free cash flow. Otherwise null."
    )

    # ---------------- governance ----------------
    auditor_opinion: AuditorOpinion | None = Field(
        None, description="Opinion type in the Independent Auditor's Report."
    )
    auditor_remarks: str | None = Field(
        None,
        description=(
            "Verbatim basis for any qualification, adverse opinion or disclaimer. "
            "Null when the opinion is unmodified."
        ),
    )

    # ---------------- self-reported uncertainty ----------------
    extraction_notes: str | None = Field(
        None,
        description=(
            "Anything ambiguous: figures restated, a line item found under an "
            "unusual name, standalone used because consolidated was absent, a "
            "value that could not be located. Null if the extraction was clean."
        ),
    )


def unit_multiplier(unit: str | None) -> float:
    """Factor converting `unit` into crore. Unknown units raise rather than guess."""
    if unit is None:
        raise ValueError("reporting_unit is missing; cannot normalise amounts")
    try:
        return _TO_CRORE[unit]
    except KeyError as e:
        raise ValueError(f"unknown reporting unit {unit!r}") from e


def to_crore(payload: AnnualReportExtraction) -> dict[str, float | None]:
    """Monetary fields converted to rupees crore; per-share fields left alone.

    Returned as a plain dict rather than a mutated model so the raw extraction
    stays intact in `extraction_attempts.payload_json` — when a number looks
    wrong six months from now, the question is always "what did the model
    actually say", and a normalised copy cannot answer it.
    """
    mult = unit_multiplier(payload.reporting_unit)
    out: dict[str, float | None] = {}
    for field in MONETARY_FIELDS:
        value = getattr(payload, field)
        out[field] = None if value is None else value * mult
    for field in PER_SHARE_FIELDS:
        out[field] = getattr(payload, field)
    return out
