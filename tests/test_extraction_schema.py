"""The JSON schema handed to the Batch API, and unit normalisation.

These are cheap tests guarding expensive failures. A malformed schema 400s a
batch of several hundred requests *after* you have assembled and paid to submit
it; a unit bug multiplies every figure in the database by 100 and looks entirely
plausible while doing so.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stockanalysis.extract.jsonschema import _UNSUPPORTED_KEYWORDS, to_api_schema
from stockanalysis.extract.schema import (
    AnnualReportExtraction,
    to_crore,
    unit_multiplier,
)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_every_object_is_closed_and_fully_required():
    schema = to_api_schema(AnnualReportExtraction)
    objects = [n for n in _walk(schema) if "properties" in n]
    assert objects, "schema has no objects — conversion produced nothing"

    for obj in objects:
        assert obj.get("additionalProperties") is False
        # Strict mode requires every property listed; optionality is expressed
        # by allowing null, not by omitting the key.
        assert set(obj["required"]) == set(obj["properties"])


def test_unsupported_keywords_are_stripped():
    schema = to_api_schema(AnnualReportExtraction)
    for node in _walk(schema):
        assert not (_UNSUPPORTED_KEYWORDS & set(node)), (
            f"unsupported keyword survived: {_UNSUPPORTED_KEYWORDS & set(node)}"
        )


def test_optional_fields_allow_null():
    schema = to_api_schema(AnnualReportExtraction)
    revenue = schema["properties"]["revenue"]
    types = {sub.get("type") for sub in revenue.get("anyOf", [])}
    assert "null" in types, "an absent line item must be expressible as null"


def test_date_format_survives_but_unknown_formats_do_not():
    schema = to_api_schema(AnnualReportExtraction)
    period_end = schema["properties"]["period_end_date"]
    formats = {sub.get("format") for sub in period_end.get("anyOf", [])}
    assert "date" in formats

    for node in _walk(schema):
        if "format" in node:
            assert node["format"] in {
                "date-time", "time", "date", "duration", "email",
                "hostname", "uri", "ipv4", "ipv6", "uuid",
            }


def test_enums_are_preserved():
    """Literal fields carry the allowed values; losing them lets the model
    invent a basis like 'BOTH', which no downstream code handles."""
    schema = to_api_schema(AnnualReportExtraction)
    dumped = str(schema)
    assert "CONSOLIDATED" in dumped and "STANDALONE" in dumped
    assert "QUALIFIED" in dumped and "UNMODIFIED" in dumped


# ----------------------------------------------------------------------
# Unit conversion
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit,value,expected_crore",
    [
        ("CRORE", 1234.5, 1234.5),
        ("LAKH", 123450.0, 1234.5),
        ("MILLION", 12345.0, 1234.5),
        ("THOUSAND", 12345000.0, 1234.5),
        ("ABSOLUTE", 12_345_000_000.0, 1234.5),
        ("BILLION", 12.345, 1234.5),
    ],
)
def test_units_convert_to_crore(unit, value, expected_crore):
    payload = AnnualReportExtraction(reporting_unit=unit, revenue=value)
    assert to_crore(payload)["revenue"] == pytest.approx(expected_crore)


def test_eps_is_never_scaled_by_the_reporting_unit():
    """The trap this exists to catch: scaling EPS by the statement's unit turns
    84.20 rupees per share into 0.0000084 and silently ruins every valuation
    factor that divides by it."""
    payload = AnnualReportExtraction(
        reporting_unit="LAKH", revenue=123450.0, eps_basic=84.20
    )
    out = to_crore(payload)
    assert out["revenue"] == pytest.approx(1234.5)
    assert out["eps_basic"] == 84.20


def test_missing_unit_raises_rather_than_assuming_one():
    with pytest.raises(ValueError, match="reporting_unit"):
        unit_multiplier(None)
    with pytest.raises(ValueError):
        to_crore(AnnualReportExtraction(revenue=100.0))


def test_unknown_unit_raises():
    with pytest.raises(ValueError, match="unknown reporting unit"):
        unit_multiplier("BAZILLION")


def test_none_values_stay_none():
    payload = AnnualReportExtraction(reporting_unit="CRORE", revenue=None)
    assert to_crore(payload)["revenue"] is None


def test_model_accepts_a_fully_populated_extraction():
    payload = AnnualReportExtraction(
        fiscal_year_label="2023-24",
        period_end_date=dt.date(2024, 3, 31),
        basis="CONSOLIDATED",
        reporting_unit="CRORE",
        currency="INR",
        revenue=1000.0,
        pat=100.0,
        total_assets=5000.0,
        total_equity=2000.0,
        total_liabilities=3000.0,
        ocf=150.0,
        capex=50.0,
        auditor_opinion="UNMODIFIED",
    )
    assert payload.basis == "CONSOLIDATED"
