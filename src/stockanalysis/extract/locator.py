"""Find the financial statements inside a 200-400 page annual report.

Roughly the first two thirds of an Indian annual report is prose: chairman's
letter, ESG narrative, directors' report, MD&A, corporate governance report.
The numbers live in the last third. Sending the whole document costs about four
times as much, and on the larger reports it exceeds the API's 32MB request
limit outright.

This step is deliberately dumb and deterministic. It does not try to decide
*which* balance sheet is the consolidated one — that judgement is the model's
job, and both sets of statements are kept precisely so the model can choose.
All the locator does is throw away the chairman's letter.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

import pymupdf

from stockanalysis.config import settings

log = logging.getLogger(__name__)


class SectionLocatorError(RuntimeError):
    """The PDF could not be narrowed to a usable set of pages."""


# Headings that mark the start of a financial statement or its notes. Weighted
# because "balance sheet" appearing on a page is far stronger evidence than a
# page merely being dense with numbers.
_ANCHORS: tuple[tuple[str, int], ...] = (
    (r"balance\s+sheet", 10),
    (r"statement\s+of\s+profit\s+and\s+loss", 10),
    (r"profit\s+and\s+loss\s+(account|statement)", 10),
    (r"statement\s+of\s+cash\s*flows?", 10),
    (r"cash\s*flow\s+statement", 10),
    (r"statement\s+of\s+changes\s+in\s+equity", 8),
    (
        r"notes?\s+(to|forming\s+part\s+of)\s+(the\s+)?"
        r"(consolidated\s+|standalone\s+)?financial\s+statements",
        9,
    ),
    (r"independent\s+auditor'?s?\s+report", 8),
    (r"significant\s+accounting\s+policies", 6),
    (r"earnings?\s+per\s+(equity\s+)?share", 5),
    (r"contingent\s+liabilit", 5),
    (r"basis\s+for\s+(qualified|adverse|disclaimer)\s+opinion", 8),
)

_COMPILED = tuple((re.compile(pat, re.IGNORECASE), weight) for pat, weight in _ANCHORS)

# Pages that are mostly prose about strategy, not statements. Negative evidence
# stops a "cash flow" mention in the MD&A from dragging in 20 pages of narrative.
_NEGATIVE = re.compile(
    r"chairman'?s?\s+(letter|message|statement)"
    r"|managing\s+director'?s?\s+(letter|message)"
    r"|sustainability\s+report"
    r"|corporate\s+social\s+responsibility"
    r"|board\s+of\s+directors\s+profile",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"\(?-?[\d,]+\.?\d*\)?")
_TOKEN = re.compile(r"\S+")


@dataclass
class LocatedSections:
    """The narrowed PDF, plus enough provenance to explain what was dropped."""

    pdf_bytes: bytes
    pages: list[int]  # 0-indexed pages of the ORIGINAL document
    total_pages: int
    anchors_found: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def size_mb(self) -> float:
        return len(self.pdf_bytes) / (1024 * 1024)

    def page_range_str(self) -> str:
        """Compact '12-18,140-201' form, for the audit trail."""
        if not self.pages:
            return ""
        spans: list[tuple[int, int]] = []
        start = prev = self.pages[0]
        for p in self.pages[1:]:
            if p == prev + 1:
                prev = p
                continue
            spans.append((start, prev))
            start = prev = p
        spans.append((start, prev))
        return ",".join(f"{a + 1}" if a == b else f"{a + 1}-{b + 1}" for a, b in spans)


def _numeric_density(text: str) -> float:
    """Fraction of whitespace tokens that look like numbers.

    A page of the notes is maybe 40% numbers; a page of the chairman's letter is
    under 5%. This is what carries continuation pages of a long note — they have
    no heading of their own, so anchors alone would drop them.
    """
    tokens = _TOKEN.findall(text)
    if len(tokens) < 20:
        return 0.0
    numbers = sum(1 for t in tokens if _NUMBER.fullmatch(t))
    return numbers / len(tokens)


def score_pages(doc: pymupdf.Document) -> tuple[list[float], list[str]]:
    """Per-page relevance score, and the anchor names that fired anywhere."""
    scores: list[float] = []
    found: list[str] = []

    for page in doc:
        text = page.get_text()
        score = 0.0
        for pattern, weight in _COMPILED:
            if pattern.search(text):
                score += weight
                if pattern.pattern not in found:
                    found.append(pattern.pattern)

        density = _numeric_density(text)
        if density > 0.15:
            score += 4.0 * min(density / 0.15, 2.0)

        if _NEGATIVE.search(text):
            score -= 8.0

        scores.append(score)

    return scores, found


def _select(scores: list[float], max_pages: int, window: int) -> list[int]:
    """Pages worth keeping: every anchor page, plus a run of pages after it.

    The trailing window matters more than it looks. A note on contingent
    liabilities is announced by a heading and then runs for pages with no
    further heading; cutting at the heading page yields a schedule with its
    numbers amputated.
    """
    keep: set[int] = set()
    n = len(scores)

    for i, s in enumerate(scores):
        if s >= 8.0:  # a real statement or notes heading
            for j in range(i, min(n, i + window + 1)):
                keep.add(j)
        elif s >= 4.0:  # dense with numbers; probably a continuation page
            keep.add(i)

    if not keep:
        return []

    if len(keep) <= max_pages:
        return sorted(keep)

    # Over budget. Drop the weakest pages rather than truncating one end —
    # truncating the tail loses the notes, and the notes are where the
    # earnings-quality signal lives.
    ranked = sorted(keep, key=lambda i: (-scores[i], i))
    return sorted(ranked[:max_pages])


def _build_pdf(doc: pymupdf.Document, pages: list[int]) -> bytes:
    out = pymupdf.open()
    for p in pages:
        # Copy page content only. Real annual reports carry AcroForm widgets and
        # link/annotation trees that are frequently malformed — Reliance's FY2026
        # report has widgets whose parent xref is missing, and copying them raises
        # ValueError deep inside insert_pdf. None of it is content we extract from,
        # so dropping it removes a whole class of failure rather than handling it.
        out.insert_pdf(doc, from_page=p, to_page=p, widgets=False, annots=False, links=False)
    buf = io.BytesIO()
    # garbage=4 drops the objects the dropped pages referenced; without it the
    # "narrowed" PDF can be nearly the size of the original.
    out.save(buf, garbage=4, deflate=True)
    out.close()
    return buf.getvalue()


def locate_sections(
    pdf_path_or_bytes: str | bytes,
    max_pages: int | None = None,
    max_mb: float | None = None,
    window: int = 6,
) -> LocatedSections:
    """Narrow an annual report to its financial statements.

    Raises `SectionLocatorError` when the report has no extractable text (a
    scanned PDF) or cannot be squeezed under the size limit. Both are review-
    queue cases: better to hand a human 3 filings than to silently extract
    from the wrong 60 pages of 300.
    """
    max_pages = max_pages or settings.extraction_max_pages
    max_mb = max_mb or settings.extraction_max_pdf_mb

    if isinstance(pdf_path_or_bytes, bytes):
        doc = pymupdf.open(stream=pdf_path_or_bytes, filetype="pdf")
    else:
        doc = pymupdf.open(pdf_path_or_bytes)

    try:
        total = doc.page_count
        scores, anchors = score_pages(doc)

        if not any(s > 0 for s in scores):
            raise SectionLocatorError(
                f"no financial-statement headings found across {total} pages — "
                f"the PDF is probably scanned images with no text layer, which "
                f"needs OCR before extraction"
            )

        pages = _select(scores, max_pages, window)
        if not pages:
            raise SectionLocatorError(
                f"headings matched but no page cleared the relevance threshold "
                f"across {total} pages"
            )

        pdf_bytes = _build_pdf(doc, pages)

        # Image-heavy reports can still be too big even at 60 pages. Shed the
        # weakest pages until it fits; give up rather than send a request the
        # API will reject.
        while len(pdf_bytes) / (1024 * 1024) > max_mb and len(pages) > 10:
            weakest = min(pages, key=lambda i: scores[i])
            pages.remove(weakest)
            pdf_bytes = _build_pdf(doc, pages)

        if len(pdf_bytes) / (1024 * 1024) > max_mb:
            raise SectionLocatorError(
                f"cannot get below {max_mb}MB: {len(pages)} pages still weigh "
                f"{len(pdf_bytes) / (1024 * 1024):.1f}MB"
            )

        result = LocatedSections(
            pdf_bytes=pdf_bytes,
            pages=pages,
            total_pages=total,
            anchors_found=anchors,
        )
        log.info(
            "located %d/%d pages (%.1fMB) — %s",
            result.page_count,
            total,
            result.size_mb,
            result.page_range_str(),
        )
        return result
    finally:
        doc.close()
