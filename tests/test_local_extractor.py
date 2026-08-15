"""Local-model extraction path.

The model itself is not exercised here — that needs LM Studio running and is a
benchmark, not a unit test. What is tested is everything around it: the text
rendering that decides how much column structure survives, the truncation
signalling, and the failure modes a small local model actually exhibits
(unreachable server, malformed JSON, schema violations). Those must degrade into
a recorded error rather than an exception, exactly as the API path does.
"""

from __future__ import annotations

import io
import json

import pymupdf
import pytest
import requests

from stockanalysis.extract.claude import ExtractionJob
from stockanalysis.extract.factory import is_local, make_extractor
from stockanalysis.extract.local import (
    DEFAULT_BASE_URL,
    LocalExtractor,
    pdf_to_text,
    render_page,
)


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 780), text, fontsize=8)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def make_job(pdf_bytes: bytes) -> ExtractionJob:
    return ExtractionJob(
        filing_id="INE000000001-2024-AR",
        isin="INE000000001",
        symbol="TESTCO",
        company="Test Company Limited",
        fiscal_year=2024,
        pdf_bytes=pdf_bytes,
    )


STATEMENT = (
    "Consolidated Balance Sheet as at 31 March 2024\n(Rs. in crore)\n"
    + "\n".join(f"Line item {i} {i * 1234}.00 {i * 987}.00" for i in range(20))
)


# ----------------------------------------------------------------------
# Text rendering
# ----------------------------------------------------------------------


def test_pages_are_flattened_to_text_with_page_markers():
    text, truncated = pdf_to_text(make_pdf([STATEMENT, STATEMENT]))
    assert not truncated
    assert "--- page 1 ---" in text and "--- page 2 ---" in text
    assert "Consolidated Balance Sheet" in text


def test_headings_survive_flattening():
    """The heading is what distinguishes the consolidated balance sheet from the
    standalone one — the single most damaging thing to lose."""
    text, _ = pdf_to_text(make_pdf([STATEMENT]))
    assert "Consolidated" in text


def test_truncation_is_reported_not_silent():
    """An extraction from a truncated document is missing whole sections. The
    caller has to be able to say so rather than leave a reviewer wondering why
    contingent liabilities came back null."""
    text, truncated = pdf_to_text(make_pdf([STATEMENT] * 10), max_chars=600)
    assert truncated
    assert len(text) <= 600


def _page_with_a_misdetected_table() -> bytes:
    """A ruled table in the corner, the real statement below it.

    This is the shape of Reliance's FY2026 cash flow statement: the detector
    latches onto some ruling lines and returns a fragment, while the statement
    itself — unruled, laid out by whitespace — is invisible to it.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    x0, y0, width, height = 60, 60, 220, 60
    for row in range(4):
        page.draw_line((x0, y0 + row * 20), (x0 + width, y0 + row * 20))
    for col in range(3):
        page.draw_line((x0 + col * 110, y0), (x0 + col * 110, y0 + height))
    for row in range(3):
        page.insert_text((x0 + 5, y0 + 14 + row * 20), f"Cell {row}", fontsize=7)

    body = "Consolidated Statement of Cash Flows for the year ended 31 March 2024\n" + "\n".join(
        f"Net Cash Flow from Operating Activities line {i} with a label  {i * 137},{i * 11}"
        for i in range(28)
    )
    page.insert_textbox(pymupdf.Rect(40, 200, 560, 780), body, fontsize=8)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def test_a_misdetected_table_does_not_delete_the_rest_of_the_page():
    """Found by running phase 1 against Reliance's FY2026 report.

    The detector returned two fragments covering 17% of the cash flow page.
    Because the table list was non-empty, rendering discarded the raw text and
    kept only those fragments — deleting the Statement of Cash Flows, and with
    it OCF. The extraction then returned `ocf: null` and scored 0.0 confidence,
    which reads exactly like a model failure rather than a text-rendering one.

    CFO/PAT and the accruals ratio exist nowhere else in the report, so this
    silently removes the two checks DESIGN calls the most reliable published
    warning signs in Indian mid-caps.
    """
    page = pymupdf.open(stream=_page_with_a_misdetected_table(), filetype="pdf")[0]
    assert list(page.find_tables().tables), "fixture must trip the table detector"

    rendered = render_page(page)
    assert "Net Cash Flow from Operating Activities" in rendered
    assert "Consolidated Statement of Cash Flows" in rendered
    assert rendered.count("line ") >= 25, "statement rows were dropped"


def test_a_well_covered_table_still_renders_as_columns():
    """The fallback must not cost us the column alignment it exists to protect:
    a page the detector reads properly still comes back pipe-separated."""
    doc = pymupdf.open()
    page = doc.new_page()
    x0, y0 = 60, 60
    for row in range(5):
        page.draw_line((x0, y0 + row * 20), (x0 + 300, y0 + row * 20))
    for col in range(4):
        page.draw_line((x0 + col * 100, y0), (x0 + col * 100, y0 + 80))
    for row in range(4):
        for col in range(3):
            page.insert_text((x0 + 5 + col * 100, y0 + 14 + row * 20), f"c{row}{col}", fontsize=7)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    rendered = render_page(pymupdf.open(stream=buf.getvalue(), filetype="pdf")[0])
    assert "|" in rendered, "column separators lost"


def test_empty_pdf_yields_no_text():
    doc = pymupdf.open()
    doc.new_page()
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    text, _ = pdf_to_text(buf.getvalue())
    assert not text.strip().replace("--- page 1 ---", "")


# ----------------------------------------------------------------------
# Failure modes — all must become a recorded error, never an exception
# ----------------------------------------------------------------------


def test_unreachable_server_gives_an_actionable_error(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    result = LocalExtractor(model="qwen").extract(make_job(make_pdf([STATEMENT])))

    assert not result.ok
    assert "LM Studio" in result.error and "lms server start" in result.error


def test_http_error_is_recorded(monkeypatch):
    class Resp:
        status_code = 400
        text = "context length exceeded"

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    result = LocalExtractor(model="qwen").extract(make_job(make_pdf([STATEMENT])))

    assert not result.ok
    assert "HTTP 400" in result.error


def test_malformed_json_is_recorded_not_raised(monkeypatch):
    """Small models miss strict schemas even with constrained decoding. It is a
    real failure mode and belongs in the bake-off numbers."""
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "sorry, I cannot"}}]}

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    result = LocalExtractor(model="qwen").extract(make_job(make_pdf([STATEMENT])))

    assert not result.ok
    assert "schema validation failed" in result.error


def test_valid_response_is_parsed_and_costs_nothing(monkeypatch):
    payload = {
        "period_end_date": "2024-03-31",
        "basis": "CONSOLIDATED",
        "reporting_unit": "CRORE",
        "revenue": 1000.0,
        "pat": 150.0,
    }

    class Resp:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 9000, "completion_tokens": 400},
            }

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    result = LocalExtractor(model="qwen").extract(make_job(make_pdf([STATEMENT])))

    assert result.ok
    assert result.payload.revenue == 1000.0
    assert result.mode == "LOCAL"
    # Zero rather than NaN, so a mixed local/API bake-off stays summable.
    assert result.cost_usd() == 0.0
    assert result.usage.input_tokens == 9000


def test_truncation_is_recorded_in_the_extraction_notes(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"reporting_unit": "CRORE", "revenue": 1000.0}
            )}}]}

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    extractor = LocalExtractor(model="qwen", max_chars=400)
    result = extractor.extract(make_job(make_pdf([STATEMENT] * 10)))

    assert result.ok
    assert "truncated" in result.payload.extraction_notes.lower()


def test_request_carries_a_strict_json_schema(monkeypatch):
    captured = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def capture(url, json=None, timeout=None):
        captured.update(json)
        return Resp()

    monkeypatch.setattr(requests, "post", capture)
    LocalExtractor(model="qwen").extract(make_job(make_pdf([STATEMENT])))

    fmt = captured["response_format"]["json_schema"]
    assert fmt["strict"] is True
    assert fmt["schema"]["additionalProperties"] is False
    # Zero temperature: this is extraction, not generation.
    assert captured["temperature"] == 0.0


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------


def test_local_prefix_selects_the_local_backend():
    assert is_local("local:qwen2.5-7b")
    assert not is_local("claude-opus-5")

    extractor = make_extractor("local:qwen2.5-7b")
    assert isinstance(extractor, LocalExtractor)
    assert extractor.model == "qwen2.5-7b"
    assert extractor.base_url == DEFAULT_BASE_URL


def test_bare_model_name_selects_the_api_backend(monkeypatch):
    from stockanalysis.extract.claude import ClaudeExtractor

    # The API backend preflights credentials on construction, so routing can
    # only be asserted with a key in place. See test_operability.py for the
    # absent-key case.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    extractor = make_extractor("claude-opus-5")
    assert isinstance(extractor, ClaudeExtractor)


def test_bare_local_prefix_needs_a_loaded_model(monkeypatch):
    monkeypatch.setattr(
        "stockanalysis.extract.local.list_local_models", lambda base_url: []
    )
    with pytest.raises(RuntimeError, match="no model loaded"):
        make_extractor("local:")
