"""XBRL results parsing — structured financials with no model in the loop.

NSE attaches an XBRL instance document to many results filings. Where it exists
this is strictly better than anything downstream of a PDF: the figures are
tagged, typed, and carry their own period and unit, so there is nothing to
misread and no confidence score to compute.

WHAT IT COVERS
--------------
Indian results XBRL follows the `in-capmkt` taxonomy and covers the P&L summary
that companies must file quarterly: revenue, expenses, profit before and after
tax, tax, EPS. It does **not** carry the full balance sheet or the cash flow
statement, so the earnings-quality signals DESIGN leans on — CFO/PAT, the
accruals ratio — remain out of reach here. Those live only in the annual report,
which is why the LLM extraction path exists at all.

ON THE ELEMENT NAMES
--------------------
The parser matches on element *local names*, ignoring namespace prefixes, and
`ELEMENT_MAP` below lists the names observed in the taxonomy. Element naming is
the part most likely to need adjustment against real filings: the mechanism is
tested here, but the specific names should be checked against a live document
before the output is trusted. `unmapped_facts()` exists for exactly that — it
reports what the parser saw and ignored, so a missing field can be diagnosed
without guesswork.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

log = logging.getLogger(__name__)

# Element local-name -> our field. Lowercased for matching; several spellings
# map to the same field because the taxonomy has changed over time.
ELEMENT_MAP: dict[str, str] = {
    "revenuefromoperations": "revenue",
    "revenuefromoperationsnet": "revenue",
    "netsalesorrevenuefromoperations": "revenue",
    "otherincome": "other_income",
    "totalincome": "total_income",
    "incomeexpenses": "total_income",
    "totalexpenses": "total_expenses",
    "expenses": "total_expenses",
    "financecosts": "interest_expense",
    "depreciationdepletionandamortisationexpense": "depreciation",
    "profitbeforetax": "profit_before_tax",
    "profitlossbeforetax": "profit_before_tax",
    "taxexpense": "tax_expense",
    "totaltaxexpense": "tax_expense",
    "profitlossforperiod": "pat",
    "profitlossfortheperiod": "pat",
    "netprofitlossfortheperiod": "pat",
    "basicearningspershare": "eps_basic",
    "basicearningslosspershare": "eps_basic",
    "dilutedearningspershare": "eps_diluted",
}

# Figures reported per share are never scaled by the instance's unit.
_PER_SHARE = frozenset({"eps_basic", "eps_diluted"})

_NS = re.compile(r"^\{[^}]*\}")


@dataclass
class XbrlFacts:
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    values: dict[str, float] = field(default_factory=dict)
    unmapped: dict[str, str] = field(default_factory=dict)
    scale_to_crore: float = 1e-7  # XBRL reports absolute rupees

    def to_crore(self) -> dict[str, float]:
        return {
            k: (v if k in _PER_SHARE else v * self.scale_to_crore)
            for k, v in self.values.items()
        }


def _local(tag: str) -> str:
    return _NS.sub("", tag).lower()


def _parse_date(text: str | None) -> dt.date | None:
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def parse_xbrl(source: str | bytes) -> XbrlFacts:
    """Extract the mapped P&L facts from an XBRL instance document.

    Facts are selected for the **latest** period present in the instance.
    Results filings routinely include the prior-year comparative and the
    year-to-date figures alongside the quarter, all in the same document; taking
    facts without checking their context is how a parser silently returns last
    year's revenue.
    """
    root = ElementTree.fromstring(
        source if isinstance(source, (str, bytes)) else str(source)
    )

    # 1. Contexts: id -> (start, end). Instants count as zero-length periods.
    contexts: dict[str, tuple[dt.date | None, dt.date | None]] = {}
    for ctx in root.iter():
        if _local(ctx.tag) != "context":
            continue
        ctx_id = ctx.get("id")
        if not ctx_id:
            continue
        start = end = None
        for node in ctx.iter():
            name = _local(node.tag)
            if name == "startdate":
                start = _parse_date(node.text)
            elif name == "enddate":
                end = _parse_date(node.text)
            elif name == "instant":
                start = end = _parse_date(node.text)
        contexts[ctx_id] = (start, end)

    if not contexts:
        raise ValueError("no XBRL contexts found; not a valid instance document")

    # 2. The reporting period is the shortest span ending on the latest date —
    #    the quarter, rather than the year-to-date figure that shares its end.
    dated = [(cid, s, e) for cid, (s, e) in contexts.items() if e is not None]
    if not dated:
        raise ValueError("no XBRL context carries an end date")

    latest_end = max(e for _, _, e in dated)
    candidates = [(cid, s, e) for cid, s, e in dated if e == latest_end]
    chosen_id, chosen_start, chosen_end = min(
        candidates,
        key=lambda t: (t[2] - t[1]).days if t[1] else 0,
    )

    # 3. Facts in that context.
    facts = XbrlFacts(period_start=chosen_start, period_end=chosen_end)
    for node in root.iter():
        ctx_ref = node.get("contextRef")
        if ctx_ref != chosen_id or node.text is None:
            continue
        name = _local(node.tag)
        text = node.text.strip()
        if not text:
            continue

        target = ELEMENT_MAP.get(name)
        if target is None:
            facts.unmapped[name] = text[:40]
            continue
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            continue
        # `sign="-"` is how XBRL negates a positively-declared element.
        if node.get("sign") == "-":
            value = -value
        facts.values[target] = value

    return facts


def unmapped_facts(source: str | bytes, limit: int = 40) -> dict[str, str]:
    """Elements the parser saw and ignored, for tuning `ELEMENT_MAP`.

    Run this against a real NSE filing before trusting the output — it turns
    "why is revenue missing" from a guess into a lookup.
    """
    facts = parse_xbrl(source)
    return dict(list(facts.unmapped.items())[:limit])
