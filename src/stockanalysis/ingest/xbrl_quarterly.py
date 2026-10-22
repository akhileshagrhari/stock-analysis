"""Quarterly results from the exchange's XBRL — what replaced `results_comparison`.

`fundamentals_quarterly` used to come from NSE's `results_comparison`, which has
not moved since the December 2024 quarter: results now go through the
integrated filing, which that endpoint never reads (see the `nse_fundamentals`
module docstring). Every results filing in either index links a tagged instance,
and the quarter is in it — so this reads the quarter from there, the same way
`xbrl_annual` reads the year.

THE WHOLE SERIES IS RE-READ, NOT JUST THE MISSING QUARTERS
----------------------------------------------------------
`results_comparison` reported *standalone total income*: RELIANCE's December
2024 quarter is 1,31,474 crore there. The instances give consolidated revenue
from operations — the definition `fundamentals_annual` already uses — and on
that basis the same quarter is nearly twice as large. A year-on-year growth rate
with one end on each definition measures the change of source, so this path
reads every quarter the index links within the window, and its rows replace the
old ones for the same period rather than sitting beside them.

The basis is settled once per company and never mixed, but not quite as on the
annual path ("consolidated if it exists at all"): here it is whichever basis
covers more of the window's quarters, consolidated on a tie. ABB filed standalone
only until September 2025; the annual rule would start its quarterly series
there and leave the year-on-year factor without a base for a year.

Banks are the exception to "revenue from operations": their taxonomy tags none.
Their top line is interest earned plus other income, which the instance tags as
total income, and that is what their rows carry — as the legacy feed did.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.ingest.nse_fundamentals import (
    FILING_DATE_ASSUMED,
    FILING_DATE_FROM_NSE,
    quarterly_filing_date,
)
from stockanalysis.ingest.xbrl import XbrlFacts, parse_xbrl
from stockanalysis.ingest.xbrl_annual import cache_path, fetch_instance

log = logging.getLogger(__name__)

SOURCE_XBRL = "XBRL"

# Longer than any quarter, shorter than any half year. An instance whose
# shortest period ending on its latest date runs past this has no quarter in it.
QUARTER_MAX_DAYS = 100

# SEBI LODR: 45 days after a quarter, 60 after the year end. Used only when the
# index carries no broadcast timestamp.
_ANNUAL_DEADLINE_DAYS = 60


@dataclass(frozen=True)
class QuarterlyFilingRef:
    isin: str
    symbol: str | None
    period_end: dt.date
    broadcast_date: dt.date | None
    relating_to: str | None
    is_consolidated: bool | None
    is_audited: bool | None
    xbrl_url: str

    @property
    def basis(self) -> str:
        return "CONSOLIDATED" if self.is_consolidated else "STANDALONE"

    @property
    def knowledge_date(self) -> dt.date:
        if self.broadcast_date is not None:
            return self.broadcast_date
        if self.relating_to == "Fourth Quarter":
            return self.period_end + dt.timedelta(days=_ANNUAL_DEADLINE_DAYS)
        return quarterly_filing_date(self.period_end)


def pending_quarterly_filings(
    db: Database,
    isins: list[str] | None = None,
    since: dt.date | None = None,
    only_missing: bool = True,
) -> list[QuarterlyFilingRef]:
    """Results filings with an XBRL instance, one basis per company, one per quarter."""
    where = ["f.xbrl_url LIKE '%.xml'"]
    params: list = []
    if isins:
        where.append(f"f.isin IN ({', '.join('?' for _ in isins)})")
        params.extend(isins)
    if since:
        where.append("f.period_end_date >= ?")
        params.append(since)

    rows = db.query(
        f"""
        SELECT f.isin, i.nse_symbol, f.period_end_date, f.broadcast_date,
               f.relating_to, f.is_consolidated, f.is_audited, f.xbrl_url
        FROM results_filings f
        LEFT JOIN instruments i ON i.isin = f.isin
        WHERE {' AND '.join(where)}
        ORDER BY f.isin, f.period_end_date
        """,
        params,
    )
    refs = [
        QuarterlyFilingRef(
            isin=r.isin,
            symbol=r.nse_symbol,
            period_end=pd.Timestamp(r.period_end_date).date(),
            broadcast_date=(
                None if pd.isna(r.broadcast_date)
                else pd.Timestamp(r.broadcast_date).date()
            ),
            relating_to=r.relating_to,
            is_consolidated=None if pd.isna(r.is_consolidated) else bool(r.is_consolidated),
            is_audited=None if pd.isna(r.is_audited) else bool(r.is_audited),
            xbrl_url=str(r.xbrl_url),
        )
        for r in rows.itertuples(index=False)
    ]
    refs = _one_basis_per_isin(refs)

    if only_missing:
        # "Already read" means *this filing* was read, not merely this quarter:
        # when the index's link for a period changes — the company's basis
        # moves, or a refiled original replaces an entry — the quarter is read
        # again rather than left on figures from a filing no longer chosen.
        have = _already_read(db, isins)
        refs = [r for r in refs if (r.isin, r.period_end, r.xbrl_url) not in have]
    return refs


def _one_basis_per_isin(refs: list[QuarterlyFilingRef]) -> list[QuarterlyFilingRef]:
    """The basis covering more of the window, consolidated on a tie. See above."""
    by_isin: dict[str, list[QuarterlyFilingRef]] = {}
    for r in refs:
        by_isin.setdefault(r.isin, []).append(r)

    out: list[QuarterlyFilingRef] = []
    for filings in by_isin.values():
        consolidated = [r for r in filings if r.is_consolidated]
        standalone = [r for r in filings if not r.is_consolidated]
        out.extend(consolidated if len(consolidated) >= len(standalone) else standalone)
    return out


def _already_read(db: Database, isins: list[str] | None) -> set[tuple[str, dt.date, str]]:
    where, params = ("", [])
    if isins:
        where, params = (f"AND isin IN ({', '.join('?' for _ in isins)})", list(isins))
    df = db.query(
        f"SELECT isin, period_end_date, xbrl_url FROM fundamentals_quarterly "
        f"WHERE source = ? AND xbrl_url IS NOT NULL {where}",
        [SOURCE_XBRL, *params],
    )
    return {
        (r.isin, pd.Timestamp(r.period_end_date).date(), r.xbrl_url)
        for r in df.itertuples(index=False)
    }


# ----------------------------------------------------------------------


@dataclass
class QuarterResult:
    ref: QuarterlyFilingRef
    row: dict | None = None
    error: str | None = None
    # Never read, as opposed to read and found unusable. Only the second says
    # anything about the filing; the first is a verdict on the network.
    fetch_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.row is not None


def ingest_quarterly_xbrl(
    db: Database,
    refs: list[QuarterlyFilingRef],
    progress: Callable[[int, int, QuarterResult], None] | None = None,
    session=None,
) -> list[QuarterResult]:
    """Fetch, parse and persist each filing's quarter."""
    out: list[QuarterResult] = []
    for i, ref in enumerate(refs, start=1):
        # Checked before the fetch, which writes the cache: only a request that
        # actually went to the exchange earns the politeness delay.
        cached = cache_path(ref.xbrl_url).exists()
        result = _ingest_one(db, ref, session)
        out.append(result)
        if progress:
            progress(i, len(refs), result)
        if not cached:
            time.sleep(settings.request_delay_seconds)
    return out


def _ingest_one(db: Database, ref: QuarterlyFilingRef, session=None) -> QuarterResult:
    try:
        raw = fetch_instance(ref.xbrl_url, session=session)
    except Exception as e:  # noqa: BLE001 - unofficial endpoint, many failure modes
        return QuarterResult(ref, error=f"fetch failed: {e}", fetch_failed=True)

    try:
        facts = parse_xbrl(raw)
    except Exception as e:  # noqa: BLE001 - malformed instances are common enough
        return QuarterResult(ref, error=f"parse failed: {type(e).__name__}: {e}")

    # The index's period is the one the row is filed under. An instance that
    # reports another would put one quarter's figures under another's date.
    if facts.period_end != ref.period_end:
        return QuarterResult(
            ref,
            error=f"instance reports a period ending {facts.period_end}, "
                  f"the index says {ref.period_end}",
        )
    span = (facts.period_end - facts.period_start).days if facts.period_start else None
    if span is None or span > QUARTER_MAX_DAYS:
        return QuarterResult(ref, error=f"no quarter in the instance ({span} days)")

    row = to_row(ref, facts)
    if row["revenue"] is None and row["pat"] is None:
        return QuarterResult(ref, error="neither revenue nor profit is tagged")

    db.upsert_df(
        "fundamentals_quarterly", pd.DataFrame([row]), ["isin", "period_end_date"]
    )
    return QuarterResult(ref, row=row)


def to_row(ref: QuarterlyFilingRef, facts: XbrlFacts) -> dict:
    v = facts.to_crore()
    revenue = v.get("revenue")
    if revenue is None:
        revenue = v.get("total_income")  # banks — see the module docstring
    return {
        "isin": ref.isin,
        "period_end_date": ref.period_end,
        "filing_date": ref.knowledge_date,
        "revenue": revenue,
        "pat": v.get("pat"),
        "eps": v.get("eps_basic"),
        "source": SOURCE_XBRL,
        "filing_date_source": (
            FILING_DATE_FROM_NSE if ref.broadcast_date else FILING_DATE_ASSUMED
        ),
        "relating_to": ref.relating_to,
        "is_consolidated": ref.is_consolidated,
        "is_audited": ref.is_audited,
        "xbrl_url": ref.xbrl_url,
    }
