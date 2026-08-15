"""Quarterly results from NSE — the free ground truth for extraction.

`NSE.results_comparison()` returns roughly the last five quarters of revenue,
net profit and EPS. It is useful twice over: as a fundamentals source in its own
right, and as an independent check on what the model read out of the PDF. Four
quarters should sum to approximately the annual figures, and when they do not,
something is wrong with the extraction — a check no amount of internal
arithmetic could provide.

**Amounts are in rupees lakhs.** Everything else in this system is in crore. The
conversion happens here, once, at the boundary. Getting it wrong is a silent
100x error in every valuation factor, which is why it is a named constant with a
test rather than an inline division.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from dataclasses import dataclass

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database

log = logging.getLogger(__name__)

# NSE reports in lakhs; we store crore. 1 crore = 100 lakh.
LAKH_TO_CRORE = 0.01

_PERIOD_END_KEYS = ("re_to_dt", "to_date", "toDate", "re_to_date")
_REVENUE_KEYS = ("re_total_inc", "re_net_sale", "total_income", "re_income")
_PAT_KEYS = ("re_net_profit", "net_profit", "re_pat", "profit_after_tax")
_EPS_KEYS = ("re_basic_eps", "re_eps", "basic_eps", "eps")
_BROADCAST_KEYS = ("re_broadcast_dt", "broadcast_dt", "re_from_dt")


@dataclass(frozen=True)
class QuarterlyResult:
    isin: str
    period_end: dt.date
    filing_date: dt.date
    revenue: float | None  # crore
    pat: float | None  # crore
    eps: float | None


def _first_float(rec: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = rec.get(k)
        if v in (None, "", "-"):
            continue
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            continue
    return None


def _first_date(rec: dict, keys: tuple[str, ...]) -> dt.date | None:
    for k in keys:
        v = rec.get(k)
        if not v:
            continue
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return dt.datetime.strptime(str(v).strip()[:11], fmt).date()
            except ValueError:
                continue
    return None


def quarterly_filing_date(period_end: dt.date) -> dt.date:
    """Conservative knowledge date for a quarterly result.

    SEBI LODR allows 45 days after the quarter end for quarterly results (60 for
    the fourth quarter / annual). Using 45 days keeps the point-in-time contract
    honest when NSE does not give a broadcast date, in the same spirit as the
    AGM-deadline fallback for annual reports.
    """
    return period_end + dt.timedelta(days=45)


def parse_results_comparison(raw: dict, isin: str) -> list[QuarterlyResult]:
    """Normalise NSE's `resCmpData` payload. Amounts converted lakhs -> crore.

    Separate from the network call so the parsing — the part that actually
    breaks when NSE changes a field name — is testable offline.

    ONE QUARTER CAN COME BACK TWICE, IN DIFFERENT UNITS
    ---------------------------------------------------
    Observed live on GAIL, quarter ending 31 December 2024:

        re_seq_num 1191981   re_total_inc 3570747     (lakhs)
        re_seq_num 1191533   re_total_inc   35707.47  (crore)

    Two filings of the same quarter, exactly 100x apart, with `re_res_type`,
    `re_face_val` and every other field identical. **Nothing in the payload
    declares the unit.** The module's lakhs assumption is right for the great
    majority of rows and wrong for these.

    That is two bugs in one. The visible one is a primary-key violation on
    (isin, period_end_date) that aborts the whole ingest. The dangerous one is
    silent: whichever row happened to be written would be multiplied by
    LAKH_TO_CRORE regardless, so a 50/50 chance of a 100x understatement in
    revenue and PAT — feeding growth, margins and the extraction cross-check
    that is supposed to catch exactly this class of error.

    Resolved by internal consistency rather than by picking a row. A company's
    revenue does not move by two orders of magnitude between quarters, so the
    quarters that came back unambiguously establish the scale, and the duplicate
    closest to that scale is the one denominated the same way. With no
    unambiguous quarter to calibrate against, the period is dropped: a missing
    quarter costs coverage, a 100x error corrupts every factor downstream of it.
    """
    rows = (raw or {}).get("resCmpData") or []
    parsed: list[tuple[QuarterlyResult, float | None]] = []

    for rec in rows:
        if not isinstance(rec, dict):
            continue
        period_end = _first_date(rec, _PERIOD_END_KEYS)
        if period_end is None:
            continue

        revenue_lakh = _first_float(rec, _REVENUE_KEYS)
        pat_lakh = _first_float(rec, _PAT_KEYS)

        broadcast = _first_date(rec, _BROADCAST_KEYS)
        filing_date = (
            broadcast
            if broadcast and broadcast > period_end
            else quarterly_filing_date(period_end)
        )

        result = QuarterlyResult(
            isin=isin,
            period_end=period_end,
            filing_date=filing_date,
            revenue=None if revenue_lakh is None else revenue_lakh * LAKH_TO_CRORE,
            pat=None if pat_lakh is None else pat_lakh * LAKH_TO_CRORE,
            # EPS is per share — never scaled by the lakh/crore conversion.
            eps=_first_float(rec, _EPS_KEYS),
        )
        parsed.append((result, _scale_of(revenue_lakh, pat_lakh)))

    out = _resolve_duplicate_periods(parsed, isin)
    return sorted(out, key=lambda q: q.period_end, reverse=True)


def _scale_of(revenue: float | None, pat: float | None) -> float | None:
    """Magnitude used to compare two filings of the same quarter.

    Revenue where present — it is the larger and steadier of the two — falling
    back to PAT, which is noisier quarter to quarter but still nowhere near 100x
    noisy.
    """
    for candidate in (revenue, pat):
        if candidate is not None and abs(candidate) > 0:
            return abs(candidate)
    return None


def _resolve_duplicate_periods(
    parsed: list[tuple[QuarterlyResult, float | None]], isin: str
) -> list[QuarterlyResult]:
    by_period: dict[dt.date, list[tuple[QuarterlyResult, float | None]]] = {}
    for result, scale in parsed:
        by_period.setdefault(result.period_end, []).append((result, scale))

    # Quarters that came back once are the reference: they cannot be ambiguous.
    reference = [
        scale
        for candidates in by_period.values()
        if len(candidates) == 1
        for _r, scale in candidates
        if scale is not None
    ]

    out: list[QuarterlyResult] = []
    for period_end, candidates in by_period.items():
        if len(candidates) == 1:
            out.append(candidates[0][0])
            continue

        if not reference:
            log.warning(
                "%s: quarter %s filed %d times in different units and no "
                "unambiguous quarter to calibrate against — dropped",
                isin, period_end, len(candidates),
            )
            continue

        median_scale = sorted(reference)[len(reference) // 2]
        usable = [(r, s) for r, s in candidates if s is not None and s > 0]
        if not usable:
            continue

        # Compare in log space: "off by 100x" is a ratio, not a difference.
        best = min(usable, key=lambda rs: abs(math.log10(rs[1] / median_scale)))
        log.warning(
            "%s: quarter %s filed %d times %s — kept the one consistent with "
            "the company's other quarters",
            isin, period_end, len(candidates),
            " and ".join(f"{s:,.2f}" for _r, s in usable),
        )
        out.append(best[0])

    return out


def ingest_quarterly(
    db: Database, isins: list[str] | None = None, delay: float | None = None
) -> int:
    """Fetch and persist quarterly results for the given instruments."""
    from nse import NSE

    delay = delay if delay is not None else settings.request_delay_seconds

    if isins:
        placeholders = ", ".join("?" for _ in isins)
        df = db.query(
            f"SELECT isin, nse_symbol FROM instruments "
            f"WHERE isin IN ({placeholders}) AND nse_symbol IS NOT NULL ORDER BY isin",
            list(isins),
        )
    else:
        df = db.query(
            "SELECT isin, nse_symbol FROM instruments "
            "WHERE nse_symbol IS NOT NULL ORDER BY isin"
        )

    total = 0
    nse = NSE(download_folder=settings.data_dir / "cache")
    try:
        for i, row in enumerate(df.itertuples(index=False), start=1):
            try:
                time.sleep(delay)
                raw = nse.results_comparison(row.nse_symbol)
            except Exception as e:  # noqa: BLE001 - unofficial API
                log.warning("results_comparison failed for %s: %s", row.nse_symbol, e)
                continue

            quarters = parse_results_comparison(raw, row.isin)
            if not quarters:
                continue

            total += db.upsert_df(
                "fundamentals_quarterly",
                pd.DataFrame(
                    [
                        {
                            "isin": q.isin,
                            "period_end_date": q.period_end,
                            "filing_date": q.filing_date,
                            "revenue": q.revenue,
                            "pat": q.pat,
                            "eps": q.eps,
                            "source": "NSE_RESULTS_COMPARISON",
                            # Upgraded to a real broadcast date later by
                            # `ingest_results_index`, if one is published.
                            "filing_date_source": FILING_DATE_ASSUMED,
                        }
                        for q in quarters
                    ]
                ),
                ["isin", "period_end_date"],
            )
            if i % 10 == 0:
                log.info("quarterly: %d/%d companies", i, len(df))
    finally:
        nse.exit()

    return total


# ----------------------------------------------------------------------
# Real knowledge dates, from the corporate filings index
# ----------------------------------------------------------------------

FILING_DATE_FROM_NSE = "NSE"
FILING_DATE_ASSUMED = "ASSUMED_LODR_DEADLINE"

_FR_SYMBOL_KEYS = ("symbol", "SYMBOL", "sym")
_FR_TO_DATE_KEYS = ("toDate", "to_date", "toDt", "period_end")
_FR_BROADCAST_KEYS = (
    "broadcastDate", "broadCastDate", "broadcast_date", "bcastDate",
    "filingDate", "creation_Date", "exchdisstime",
)
_FR_RELATING_KEYS = ("relatingTo", "relating_to", "reInd")
_FR_XBRL_KEYS = ("xbrl", "xbrl_attachment", "xbrlAttachment", "xbrlFile")
_FR_CONSOLIDATED_KEYS = ("consolidated", "consol", "reConsolidated")
_FR_AUDITED_KEYS = ("audited", "auditedUnaudited", "reAudited")


@dataclass(frozen=True)
class ResultsFiling:
    """One entry from NSE's financial-results filing index."""

    symbol: str
    period_end: dt.date
    broadcast_date: dt.date | None
    relating_to: str | None
    is_consolidated: bool | None
    is_audited: bool | None
    xbrl_url: str | None


def _truthy(value: object) -> bool | None:
    """NSE encodes these flags as words, not booleans, and inconsistently."""
    if value in (None, ""):
        return None
    s = str(value).strip().lower()
    if s in ("consolidated", "audited", "yes", "y", "true", "1"):
        return True
    if s in ("non-consolidated", "standalone", "un-audited", "unaudited",
             "no", "n", "false", "0"):
        return False
    return None


def parse_financial_results(raw: list[dict] | dict) -> list[ResultsFiling]:
    """Normalise the financial-results filing index.

    This index is the only free source of *real* knowledge dates for quarterly
    numbers. Everywhere else in this system a missing date falls back to a
    statutory deadline, which is safe but late; here we can use what actually
    happened.
    """
    records = raw if isinstance(raw, list) else next(
        (v for v in (raw or {}).values() if isinstance(v, list)), []
    )

    out: list[ResultsFiling] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        symbol = None
        for k in _FR_SYMBOL_KEYS:
            if rec.get(k):
                symbol = str(rec[k]).strip()
                break
        period_end = _first_date(rec, _FR_TO_DATE_KEYS)
        if not symbol or period_end is None:
            continue

        broadcast = _first_date(rec, _FR_BROADCAST_KEYS)
        # A broadcast date at or before the period end cannot be when the
        # numbers became public. Discard it rather than import lookahead.
        if broadcast is not None and broadcast <= period_end:
            broadcast = None

        xbrl = None
        for k in _FR_XBRL_KEYS:
            v = rec.get(k)
            if v and str(v).lower().startswith("http"):
                xbrl = str(v).strip()
                break

        out.append(
            ResultsFiling(
                symbol=symbol,
                period_end=period_end,
                broadcast_date=broadcast,
                relating_to=next(
                    (str(rec[k]) for k in _FR_RELATING_KEYS if rec.get(k)), None
                ),
                is_consolidated=_truthy(
                    next((rec[k] for k in _FR_CONSOLIDATED_KEYS if k in rec), None)
                ),
                is_audited=_truthy(
                    next((rec[k] for k in _FR_AUDITED_KEYS if k in rec), None)
                ),
                xbrl_url=xbrl,
            )
        )
    return out


def apply_results_filing_index(
    db: Database, filings: list[ResultsFiling]
) -> int:
    """Replace assumed quarterly knowledge dates with real broadcast dates.

    Matches on (symbol -> isin, period_end). Rows with no matching filing keep
    the LODR-deadline fallback and stay labelled as assumed, so the split
    between measured and inferred dates remains visible in `status`.

    Only the *quarterly* table is updated. The annual-report knowledge date is
    deliberately left alone: a results filing publishes the P&L summary months
    before the full annual report is available, so borrowing its date for
    figures that only exist in the report — operating cash flow, contingent
    liabilities, the auditor's opinion — would make them visible before anyone
    could have read them.
    """
    if not filings:
        return 0

    symbols = db.query(
        "SELECT isin, nse_symbol FROM instruments WHERE nse_symbol IS NOT NULL"
    )
    by_symbol = dict(zip(symbols["nse_symbol"], symbols["isin"], strict=False))

    updated = 0
    for f in filings:
        isin = by_symbol.get(f.symbol)
        if isin is None or f.broadcast_date is None:
            continue
        cur = db.conn.execute(
            "UPDATE fundamentals_quarterly SET "
            "  filing_date = ?, filing_date_source = ?, relating_to = ?, "
            "  is_consolidated = ?, is_audited = ?, xbrl_url = COALESCE(?, xbrl_url) "
            "WHERE isin = ? AND period_end_date = ?",
            [
                f.broadcast_date, FILING_DATE_FROM_NSE, f.relating_to,
                f.is_consolidated, f.is_audited, f.xbrl_url, isin, f.period_end,
            ],
        )
        updated += cur.fetchall()[0][0] if cur.description else 1

    return updated


def ingest_results_index(
    db: Database,
    from_date: dt.date | None = None,
    to_date: dt.date | None = None,
    period: str = "quarterly",
) -> int:
    """Fetch the whole filing index for a date range and apply real dates.

    Deliberately one request per date range rather than one per symbol. The
    endpoint is index-wide, so a 100-company universe costs a handful of calls
    instead of a hundred — which matters when getting IP-blocked is the main
    operational risk of this phase.
    """
    from nse import NSE

    to_date = to_date or dt.date.today()
    from_date = from_date or (to_date - dt.timedelta(days=365 * 3))

    nse = NSE(download_folder=settings.data_dir / "cache")
    applied = 0
    try:
        # NSE caps the span it will return; walk it in 90-day windows.
        window_start = from_date
        while window_start < to_date:
            window_end = min(window_start + dt.timedelta(days=90), to_date)
            try:
                time.sleep(settings.request_delay_seconds)
                raw = nse.financial_results(
                    segment="equities",
                    period=period,
                    from_date=dt.datetime.combine(window_start, dt.time.min),
                    to_date=dt.datetime.combine(window_end, dt.time.min),
                )
            except Exception as e:  # noqa: BLE001 - unofficial API
                log.warning("financial_results %s..%s failed: %s",
                            window_start, window_end, e)
                window_start = window_end
                continue

            filings = parse_financial_results(raw)
            applied += apply_results_filing_index(db, filings)
            log.info("results index %s..%s: %d filings", window_start, window_end,
                     len(filings))
            window_start = window_end
    finally:
        nse.exit()

    return applied


def quarters_for_fiscal_year(
    db: Database, isin: str, fiscal_year: int
) -> list[dict]:
    """The four quarters making up an Indian fiscal year, in crore.

    Returned in the shape `validate._nse_checks` expects. Indian FY2024 runs
    1 April 2023 to 31 March 2024.
    """
    start = dt.date(fiscal_year - 1, 4, 1)
    end = dt.date(fiscal_year, 3, 31)
    df = db.query(
        "SELECT period_end_date, revenue, pat FROM fundamentals_quarterly "
        "WHERE isin = ? AND period_end_date > ? AND period_end_date <= ? "
        "ORDER BY period_end_date",
        [isin, start - dt.timedelta(days=1), end],
    )
    return [
        {
            "period_end": r.period_end_date,
            "revenue": None if pd.isna(r.revenue) else float(r.revenue),
            "pat": None if pd.isna(r.pat) else float(r.pat),
        }
        for r in df.itertuples(index=False)
    ]
