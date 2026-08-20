"""XBRL results parsing — structured financials with no model in the loop.

NSE attaches an XBRL instance document to many results filings. Where it exists
this is strictly better than anything downstream of a PDF: the figures are
tagged, typed, and carry their own period and unit, so there is nothing to
misread and no confidence score to compute.

WHAT IT COVERS
--------------
Indian results XBRL follows the `in-capmkt` taxonomy. Every filing carries the
P&L summary: revenue, expenses, profit before and after tax, tax, EPS.

**The half-yearly and annual filings carry more than that** — the full balance
sheet and the cash flow statement, because SEBI LODR requires both alongside
audited annual results. `Assets`, `Equity`, `BorrowingsCurrent/Noncurrent`,
`CashFlowsFromUsedInOperatingActivities` and
`PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` are all
present and tagged. That reaches the earnings-quality signals DESIGN leans on —
CFO/PAT and the accruals ratio — with no model in the loop and nothing to
misread, which is why `parse_annual_xbrl` exists and why the LLM extraction path
is now a fallback rather than the only route to a balance sheet.

What XBRL does *not* carry is `contingent_liabilities` — there is no element for
it in the taxonomy. That one field still comes from the annual report PDF, and
until it does it must read as unknown rather than zero.

CONTEXTS, AND WHICH DATES TO BELIEVE
------------------------------------
One instance holds several periods at once: the current quarter, the year to
date, the prior-year comparative, and an instant for the balance sheet. NSE's
convention names them `OneD` (current duration), `FourD` (year-to-date) and
`OneI` (instant).

**The `<xbrli:period>` on the context is not reliable.** Both filings this parser
was built against copy the quarter's dates onto the year-to-date context:

    Infosys FY2024   OneD and FourD both declare 2024-01-01 .. 2024-03-31
    Zydus H1 FY2025  OneD and FourD both declare 2024-07-01 .. 2024-09-30

Trusting that would read Infosys' 1,28,933 crore of full-year revenue as a
quarter, or — worse in the other direction — accept Zydus' six months as a year.
Either error is invisible downstream: the figure is internally consistent and
every margin computed from it still holds, only the level is wrong.

The period each context actually describes is carried as a *fact* instead —
`DateOfStartOfReportingPeriod` and `DateOfEndOfReportingPeriod`, tagged against
the context they belong to — and those are honest in both filings:

    Infosys FourD    2023-04-01 .. 2024-03-31   365 days, a year
    Zydus   FourD    2024-04-01 .. 2024-09-30   182 days, half of one

So the reported period wins, and the context's own dates are only a fallback for
instances that omit it. Dimensional contexts — the `OneOperatingExpenses01D`
family, which break expenses out by line — are excluded throughout; they restate
parts of a total that is already tagged.

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
    # The Ind AS results taxonomy tags total income as plain `Income`, sitting
    # directly above `Expenses`. Verified against both reference filings:
    # revenue 26,206 + other income 3,267 == income 29,473.
    "income": "total_income",
    "totalexpenses": "total_expenses",
    "expenses": "total_expenses",
    "financecosts": "interest_expense",
    "depreciationdepletionandamortisationexpense": "depreciation",
    "profitbeforetax": "profit_before_tax",
    "profitlossbeforetax": "profit_before_tax",
    "taxexpense": "tax_expense",
    "totaltaxexpense": "tax_expense",
    # The group's whole profit. For a standalone filing that *is* PAT; for a
    # consolidated one it is PAT plus the minority's share, so it is kept under
    # its own name and `_derive` decides. Mapping both to `pat` would make the
    # answer depend on which element the filer happened to tag last.
    "profitlossforperiod": "profit_for_period",
    "profitlossfortheperiod": "profit_for_period",
    "netprofitlossfortheperiod": "profit_for_period",
    # The consolidated split, tagged explicitly where it exists. L&T FY2024:
    # 13,059 to owners + 2,488 to minorities == 15,547 for the group.
    "profitorlossattributabletoownersofparent": "pat",
    "profitorlossattributabletononcontrollinginterests": "non_controlling_interest",
    "basicearningspershare": "eps_basic",
    "basicearningslosspershare": "eps_basic",
    "basicearningslosspersharefromcontinuinganddiscontinuedoperations": "eps_basic",
    "dilutedearningspershare": "eps_diluted",
    "dilutedearningslosspersharefromcontinuinganddiscontinuedoperations": "eps_diluted",
    "shareofprofitlossofassociatesandjointventuresaccountedforusingequitymethod": (
        "share_of_associates"
    ),
    # ---- balance sheet (instant context) ----
    "assets": "total_assets",
    "equity": "total_equity",
    "equityandliabilities": "equity_and_liabilities",
    "borrowingscurrent": "borrowings_current",
    "borrowingsnoncurrent": "borrowings_noncurrent",
    "cashandcashequivalents": "cash",
    # ---- cash flow (year-to-date context) ----
    "cashflowsfromusedinoperatingactivities": "ocf",
    "purchaseofpropertyplantandequipmentclassifiedasinvestingactivities": "capex",
}

# Facts that are text rather than an amount, and are read separately.
_ROUNDING_ELEMENT = "levelofroundingusedinfinancialstatements"
_AUDITED_ELEMENT = "whetherresultsareauditedorunaudited"
_OPINION_ELEMENT = "declarationofunmodifiedopinionorstatementonimpactofauditqualification"
# The period a context actually describes. See the module docstring.
_PERIOD_START_ELEMENT = "dateofstartofreportingperiod"
_PERIOD_END_ELEMENT = "dateofendofreportingperiod"

# A duration this long ending at the period end is a year, not a quarter. Slack
# for the 52/53-week retailers and for a first year after incorporation that
# runs short of twelve months.
ANNUAL_MIN_DAYS = 300

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

    _fill_pat(facts.values)
    return facts


def _fill_pat(values: dict[str, float]) -> None:
    """Shareholders' profit, where the filing tags only the group's.

    A standalone filing has no owners/minorities split to tag, so the group's
    profit *is* the shareholders' profit. Only ever a fallback: overwriting a
    tagged attributable figure with the group total is what makes a consolidated
    PAT too large by exactly the minority's share.
    """
    if values.get("pat") is None and "profit_for_period" in values:
        values["pat"] = values["profit_for_period"]


class NotAnnualFiling(ValueError):
    """The instance carries no twelve-month context.

    Raised rather than returning the quarter, because a quarter written into
    `fundamentals_annual` is a number that looks right and is four times too
    small — every margin holds, every ratio survives, and only the level is
    wrong. Nothing downstream distinguishes that from a bad year.
    """


@dataclass
class AnnualFacts:
    """One fiscal year, assembled from an instance's annual and instant contexts."""

    period_start: dt.date | None = None
    period_end: dt.date | None = None
    values: dict[str, float] = field(default_factory=dict)
    unmapped: dict[str, str] = field(default_factory=dict)
    audited: bool | None = None
    opinion_note: str | None = None
    scale_to_crore: float = 1e-7

    def to_crore(self) -> dict[str, float]:
        return {
            k: (v if k in _PER_SHARE else v * self.scale_to_crore)
            for k, v in self.values.items()
        }


@dataclass(frozen=True)
class _Context:
    cid: str
    start: dt.date | None
    end: dt.date | None
    instant: bool

    @property
    def span_days(self) -> int | None:
        if self.start is None or self.end is None:
            return None
        return (self.end - self.start).days


def _contexts(root) -> list[_Context]:
    """Non-dimensional contexts only. See the module docstring on why."""
    out: list[_Context] = []
    for node in root.iter():
        if _local(node.tag) != "context":
            continue
        cid = node.get("id")
        if cid is None:
            continue
        if any(_local(n.tag) == "explicitmember" for n in node.iter()):
            continue
        start = end = None
        instant = False
        for child in node.iter():
            name = _local(child.tag)
            if name == "startdate":
                start = _parse_date(child.text)
            elif name == "enddate":
                end = _parse_date(child.text)
            elif name == "instant":
                start = end = _parse_date(child.text)
                instant = True
        out.append(_Context(cid, start, end, instant))
    return out


def _reported_periods(root) -> dict[str, tuple[dt.date | None, dt.date | None]]:
    """Each context's period as the filing *states* it, keyed by context id.

    See the module docstring: this is the trustworthy source, and the context's
    own `<xbrli:period>` is not.
    """
    out: dict[str, list[dt.date | None]] = {}
    for node in root.iter():
        name = _local(node.tag)
        if name not in (_PERIOD_START_ELEMENT, _PERIOD_END_ELEMENT):
            continue
        cid = node.get("contextRef")
        if cid is None:
            continue
        pair = out.setdefault(cid, [None, None])
        pair[0 if name == _PERIOD_START_ELEMENT else 1] = _parse_date(node.text)
    return {cid: (pair[0], pair[1]) for cid, pair in out.items()}


def _pick_annual(durations: list[_Context]) -> _Context:
    """The twelve-month context, by the period the filing reports for it."""
    if not durations:
        raise NotAnnualFiling("instance carries no duration context")

    latest_end = max(c.end for c in durations if c.end is not None)
    ending_latest = [c for c in durations if c.end == latest_end]

    annual = [
        c for c in ending_latest
        if c.span_days is not None and c.span_days >= ANNUAL_MIN_DAYS
    ]
    if annual:
        # Longest, so a filing carrying both the year and a shorter stub picks
        # the year rather than whichever came first in the document.
        return max(annual, key=lambda c: c.span_days or 0)

    longest = max((c.span_days or 0) for c in ending_latest)
    raise NotAnnualFiling(
        f"longest period ending {latest_end} spans {longest} days, short of the "
        f"{ANNUAL_MIN_DAYS} an annual filing needs — this is a quarterly or "
        f"half-yearly filing"
    )


def parse_annual_xbrl(source: str | bytes) -> AnnualFacts:
    """A full fiscal year — P&L, cash flow and balance sheet — from one instance.

    Facts are drawn from two contexts: the annual duration for everything that
    accumulates over the year, and the period-end instant for the balance sheet.
    Merging them is safe because the taxonomy gives a flow and a stock different
    element names; nothing is written twice.
    """
    root = ElementTree.fromstring(source)
    contexts = _contexts(root)
    if not contexts:
        raise ValueError("no XBRL contexts found; not a valid instance document")

    # Override each duration context's declared period with the one the filing
    # reports for it. Instants are left alone — the balance-sheet date is the
    # one date in the document that is reliable as declared.
    reported = _reported_periods(root)
    contexts = [
        c if c.instant or c.cid not in reported
        else _Context(c.cid, *reported[c.cid], instant=False)
        for c in contexts
    ]

    durations = [c for c in contexts if not c.instant and c.end is not None]
    annual = _pick_annual(durations)

    instants = [c for c in contexts if c.instant and c.end is not None]
    instant = (
        max(instants, key=lambda c: c.end or dt.date.min) if instants else None
    )
    if instant is None:
        raise NotAnnualFiling(
            "instance carries no instant context, so it has no balance sheet — "
            "this is a quarterly filing, not an annual one"
        )

    wanted = {annual.cid, instant.cid}
    facts = AnnualFacts(
        period_start=annual.start,
        # The instant is the balance-sheet date, and it is the one date in the
        # document that is reliable even where the duration dates are not.
        period_end=instant.end,
    )

    for node in root.iter():
        ctx_ref = node.get("contextRef")
        text = (node.text or "").strip()
        if not text:
            continue
        name = _local(node.tag)

        if name == _AUDITED_ELEMENT and facts.audited is None:
            facts.audited = text.strip().lower().startswith("audited")
            continue
        if name == _OPINION_ELEMENT and facts.opinion_note is None:
            facts.opinion_note = text
            continue
        if ctx_ref not in wanted:
            continue

        target = ELEMENT_MAP.get(name)
        if target is None:
            facts.unmapped[name] = text[:40]
            continue
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            continue
        if node.get("sign") == "-":
            value = -value
        facts.values[target] = value

    _derive(facts)
    return facts


def _derive(facts: AnnualFacts) -> None:
    """Fields the factor model needs that the taxonomy splits or omits.

    Total debt is the sum of the two borrowings buckets — the taxonomy has no
    combined element, and DESIGN's leverage factors want the pair. Total
    liabilities is `EquityAndLiabilities - Equity` rather than a tagged figure,
    which is the same identity `validate` checks and so is deliberately *not*
    used to satisfy that check: `equity_and_liabilities` is kept alongside it so
    the check still compares two independently reported numbers.
    """
    _fill_pat(facts.values)

    current = facts.values.get("borrowings_current")
    noncurrent = facts.values.get("borrowings_noncurrent")
    if current is not None or noncurrent is not None:
        facts.values["total_debt"] = (current or 0.0) + (noncurrent or 0.0)

    both = facts.values.get("equity_and_liabilities"), facts.values.get("total_equity")
    if all(v is not None for v in both):
        facts.values["total_liabilities"] = both[0] - both[1]


def unmapped_facts(source: str | bytes, limit: int = 40) -> dict[str, str]:
    """Elements the parser saw and ignored, for tuning `ELEMENT_MAP`.

    Run this against a real NSE filing before trusting the output — it turns
    "why is revenue missing" from a guess into a lookup.
    """
    facts = parse_xbrl(source)
    return dict(list(facts.unmapped.items())[:limit])
