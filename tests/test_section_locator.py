"""Section locator, against synthetic annual reports.

Synthetic because the property being tested — "keeps the statements, drops the
chairman's letter" — has to hold for any report, and generated PDFs let us build
the adversarial cases directly: a narrative page that mentions cash flow, a
notes section that runs for pages with no heading of its own, a document with no
text layer at all.
"""

from __future__ import annotations

import io

import pymupdf
import pytest

from stockanalysis.extract.locator import (
    SectionLocatorError,
    locate_sections,
    score_pages,
)

NARRATIVE = """\
Chairman's Message to the Shareholders

It gives me great pleasure to present the annual report for the year. Our
company has continued to deliver on its strategic priorities, investing in our
people and our communities while maintaining a disciplined approach to growth.
The board remains confident in the long term prospects of the business and in
the management team's ability to execute against the plan we set out.
"""

# A narrative page that mentions the words the anchors look for. Without the
# negative-evidence rule this drags in twenty pages of prose.
DECOY = """\
Management Discussion and Analysis

Our cash flow position remained healthy through the year, and the balance sheet
continues to provide the flexibility to fund our capital programme. We discuss
the movements in our profit and loss in more detail in the sections that follow,
alongside our sustainability report and corporate social responsibility update.
"""


def _numbers_block(seed: int) -> str:
    rows = []
    for i in range(22):
        a = (seed * 977 + i * 131) % 90000 + 1000
        b = (seed * 331 + i * 197) % 90000 + 1000
        rows.append(f"Line item {i} {a:,}.00 {b:,}.00 {a / 7:,.2f} {b / 3:,.2f}")
    return "\n".join(rows)


def build_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 560, 780), text, fontsize=8)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@pytest.fixture
def annual_report() -> bytes:
    """30 pages: 20 of narrative, then the statements and notes."""
    pages = [NARRATIVE] * 18 + [DECOY, DECOY]
    pages += [
        "Independent Auditor's Report\n\nOpinion\n\nWe have audited...",
        "Consolidated Balance Sheet as at 31 March 2024\n(Rs. in crore)\n"
        + _numbers_block(1),
        "Consolidated Statement of Profit and Loss for the year ended 31 March 2024\n"
        + _numbers_block(2),
        "Consolidated Statement of Cash Flows for the year ended 31 March 2024\n"
        + _numbers_block(3),
        "Notes to the Consolidated Financial Statements\n" + _numbers_block(4),
        # Continuation pages: no heading of their own, only dense numbers.
        _numbers_block(5),
        _numbers_block(6),
        "Contingent liabilities and commitments\n" + _numbers_block(7),
        _numbers_block(8),
        _numbers_block(9),
    ]
    return build_pdf(pages)


def test_statement_pages_are_kept(annual_report):
    located = locate_sections(annual_report, max_pages=60)
    for page in range(20, 30):
        assert page in located.pages, f"dropped statement page {page}"


def test_narrative_pages_are_dropped(annual_report):
    located = locate_sections(annual_report, max_pages=60)
    # The first 18 pages are pure chairman's letter with no numbers.
    assert not (set(range(0, 18)) & set(located.pages))


def test_decoy_narrative_does_not_drag_in_the_prose(annual_report):
    """An MD&A page mentioning 'cash flow' and 'balance sheet' scores negative
    on the narrative markers, so it does not open a six-page window into the
    chairman's letter."""
    located = locate_sections(annual_report, max_pages=60)
    assert 18 not in located.pages and 19 not in located.pages


def test_notes_continuation_pages_survive(annual_report):
    """Pages 25-26 and 28-29 have no heading, only numbers. Anchor matching
    alone would drop them and amputate the schedules the earnings-quality
    checks depend on."""
    located = locate_sections(annual_report, max_pages=60)
    assert {25, 26, 28, 29} <= set(located.pages)


def test_output_is_a_valid_smaller_pdf(annual_report):
    located = locate_sections(annual_report, max_pages=60)
    with pymupdf.open(stream=located.pdf_bytes, filetype="pdf") as doc:
        assert doc.page_count == located.page_count
    assert located.total_pages == 30
    assert located.page_count < located.total_pages


def test_page_budget_is_respected_and_keeps_the_strongest(annual_report):
    located = locate_sections(annual_report, max_pages=5)
    assert located.page_count == 5
    # Under pressure it keeps the headed statements over bare number pages.
    assert {21, 22, 23} <= set(located.pages)


def test_page_range_string_is_human_readable(annual_report):
    located = locate_sections(annual_report, max_pages=60)
    rendered = located.page_range_str()
    assert "-" in rendered
    # 1-indexed for humans reading a PDF viewer.
    assert rendered.split(",")[0].split("-")[0] == str(min(located.pages) + 1)


def test_scanned_pdf_raises_rather_than_guessing():
    """No text layer means no anchors. Sending 60 arbitrary pages of 300 would
    be worse than failing, so this is a review case."""
    doc = pymupdf.open()
    for _ in range(30):
        doc.new_page()
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    with pytest.raises(SectionLocatorError, match="scanned|no financial-statement"):
        locate_sections(buf.getvalue())


def test_pure_narrative_document_raises():
    with pytest.raises(SectionLocatorError):
        locate_sections(build_pdf([NARRATIVE] * 12))


def test_size_limit_trims_further(annual_report):
    """The API rejects requests over 32MB. A report that will not fit must be
    trimmed or refused, never submitted."""
    with pytest.raises(SectionLocatorError, match="cannot get below"):
        locate_sections(annual_report, max_pages=60, max_mb=0.000001)


def test_malformed_form_widgets_do_not_break_the_narrowing(annual_report):
    """Real annual reports carry AcroForm widgets, and theirs are often broken.

    Reliance's FY2026 report has widgets whose parent field's /Kids array does
    not list them; copying those pages raises ValueError from inside
    insert_pdf, before any extraction is attempted. The locator copies page
    content only, so the shape of the form tree cannot decide whether a filing
    is extractable.
    """
    doc = pymupdf.open(stream=annual_report, filetype="pdf")
    for page in doc:
        widget = pymupdf.Widget()
        widget.field_name = f"f{page.number}"
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        widget.rect = pymupdf.Rect(400, 20, 560, 40)
        page.add_widget(widget)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    # Orphan the widgets from the field that claims them.
    doc = pymupdf.open(stream=buf.getvalue(), filetype="pdf")
    parent = doc.get_new_xref()
    doc.update_object(parent, "<< /FT /Tx /T (grp) /Kids [ ] >>")
    for page in doc:
        for widget in page.widgets():
            doc.xref_set_key(widget.xref, "Parent", f"{parent} 0 R")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    located = locate_sections(buf.getvalue(), max_pages=60)
    assert {21, 22, 23} <= set(located.pages)


def test_scores_rank_statements_above_narrative(annual_report):
    with pymupdf.open(stream=annual_report, filetype="pdf") as doc:
        scores, anchors = score_pages(doc)
    assert max(scores[20:30]) > max(scores[0:18])
    assert any("balance" in a for a in anchors)
