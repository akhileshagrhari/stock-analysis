"""Quarterly shareholding patterns from NSE.

Free, structured, and needs no LLM — which makes this the cheapest genuinely
useful fundamental data in the system.

WHAT THIS DOES AND DOES NOT SUPPORT
-----------------------------------
DESIGN §6.2 lists two promoter-related red flags. This endpoint supports one of
them and not the other:

  supported     promoter holding falling for three consecutive quarters
  NOT supported promoter pledge > 25%, or rising sharply quarter-on-quarter

`NSE.shareholding()` returns the holding split — promoter and promoter group,
public, employee trusts — but carries no pledged-shares figure. Pledge data is
disclosed separately (the SAST/encumbrance filings) and is not reachable from
here. `promoter_pledged_pct` therefore stays NULL, and any factor that reads it
must treat NULL as "unknown", never as zero. Reading a missing pledge as zero
would turn the most informative red flag in the Indian mid-cap universe into a
clean bill of health for exactly the companies it exists to catch.
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

log = logging.getLogger(__name__)

DISCLOSED_FROM_NSE = "NSE"
DISCLOSED_ASSUMED = "ASSUMED_LODR_DEADLINE"

# SEBI LODR Regulation 31: shareholding pattern within 21 days of quarter end.
LODR_DISCLOSURE_DAYS = 21

_AS_ON_KEYS = ("date", "as_on_date", "asOnDate", "quarter_end")
_PROMOTER_KEYS = ("pr_and_prgrp", "promoterAndPromoterGroup", "promoter_pct")
_PUBLIC_KEYS = ("public_val", "public", "publicShareholding", "public_pct")
_EMPLOYEE_TRUST_KEYS = ("employeeTrusts", "employee_trusts", "shrhldng_empTrust")
# Present on some responses, absent on others. Parsed when available rather
# than assumed.
_FII_KEYS = ("fii", "fiiHolding", "foreignPortfolioInvestors")
_DII_KEYS = ("dii", "diiHolding", "domesticInstitutionalInvestors")
_PLEDGE_KEYS = ("pledged", "pledgedShares", "encumbered", "pr_pledged")


@dataclass(frozen=True)
class ShareholdingRecord:
    isin: str
    quarter_end: dt.date
    disclosed_date: dt.date
    disclosed_date_source: str
    promoter_pct: float | None
    promoter_pledged_pct: float | None
    fii_pct: float | None
    dii_pct: float | None
    public_pct: float | None
    employee_trust_pct: float | None


def _first_float(rec: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = rec.get(k)
        if v in (None, "", "-"):
            continue
        try:
            return float(str(v).replace(",", "").replace("%", ""))
        except ValueError:
            continue
    return None


def _first_date(rec: dict, keys: tuple[str, ...]) -> dt.date | None:
    for k in keys:
        v = rec.get(k)
        if not v:
            continue
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%B-%Y"):
            try:
                return dt.datetime.strptime(str(v).strip()[:11], fmt).date()
            except ValueError:
                continue
    return None


def assumed_disclosure_date(quarter_end: dt.date) -> dt.date:
    """Conservative knowledge date: the LODR filing deadline.

    Same reasoning as the annual-report AGM fallback — the shareholding pattern
    as at 31 March was not public on 31 March, and treating it as though it were
    hands a backtest three weeks of free information.
    """
    return quarter_end + dt.timedelta(days=LODR_DISCLOSURE_DAYS)


def parse_shareholding(raw: list[dict] | dict, isin: str) -> list[ShareholdingRecord]:
    """Normalise NSE's shareholding response, newest quarter first.

    Split out from the network call so the parsing — the part that breaks when
    NSE renames a field — is testable without a live session.
    """
    if isinstance(raw, dict):
        records = next(
            (v for v in raw.values() if isinstance(v, list)), []
        )
    else:
        records = raw or []

    out: dict[dt.date, ShareholdingRecord] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        quarter_end = _first_date(rec, _AS_ON_KEYS)
        if quarter_end is None:
            continue

        out[quarter_end] = ShareholdingRecord(
            isin=isin,
            quarter_end=quarter_end,
            disclosed_date=assumed_disclosure_date(quarter_end),
            disclosed_date_source=DISCLOSED_ASSUMED,
            promoter_pct=_first_float(rec, _PROMOTER_KEYS),
            # Not in this endpoint. NULL means unknown, never zero — see the
            # module docstring.
            promoter_pledged_pct=_first_float(rec, _PLEDGE_KEYS),
            fii_pct=_first_float(rec, _FII_KEYS),
            dii_pct=_first_float(rec, _DII_KEYS),
            public_pct=_first_float(rec, _PUBLIC_KEYS),
            employee_trust_pct=_first_float(rec, _EMPLOYEE_TRUST_KEYS),
        )

    return sorted(out.values(), key=lambda r: r.quarter_end, reverse=True)


def promoter_holding_trend(db: Database, isin: str, as_of: dt.date) -> list[float]:
    """Promoter holding for the quarters disclosed on or before `as_of`.

    Newest first, so a falling-for-three-quarters check is
    `t[0] < t[1] < t[2] < t[3]`. Reads through the knowledge date, so a
    backtest cannot see a disclosure that had not happened yet.
    """
    df = db.query(
        "SELECT promoter_pct FROM shareholding "
        "WHERE isin = ? AND disclosed_date <= ? AND promoter_pct IS NOT NULL "
        "ORDER BY quarter_end DESC LIMIT 8",
        [isin, as_of],
    )
    return [float(v) for v in df["promoter_pct"]]


def ingest_shareholding(
    db: Database,
    isins: list[str] | None = None,
    delay: float | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """Fetch and persist quarterly shareholding for the given instruments.

    `progress` is called as `progress(index, total, symbol)` before each
    request, 1-based — see `ingest.prices.ingest_prices`.
    """
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
            if progress:
                progress(i, len(df), row.nse_symbol)
            try:
                time.sleep(delay)
                raw = nse.shareholding(row.nse_symbol)
            except Exception as e:  # noqa: BLE001 - unofficial API, fails many ways
                log.warning("shareholding failed for %s: %s", row.nse_symbol, e)
                continue

            records = parse_shareholding(raw, row.isin)
            if not records:
                continue

            total += db.upsert_df(
                "shareholding",
                pd.DataFrame(
                    [
                        {
                            "isin": r.isin,
                            "quarter_end": r.quarter_end,
                            "disclosed_date": r.disclosed_date,
                            "disclosed_date_source": r.disclosed_date_source,
                            "promoter_pct": r.promoter_pct,
                            "promoter_pledged_pct": r.promoter_pledged_pct,
                            "fii_pct": r.fii_pct,
                            "dii_pct": r.dii_pct,
                            "public_pct": r.public_pct,
                            "employee_trust_pct": r.employee_trust_pct,
                        }
                        for r in records
                    ]
                ),
                ["isin", "quarter_end"],
            )
            if i % 10 == 0:
                log.info("shareholding: %d/%d companies", i, len(df))
    finally:
        nse.exit()

    return total
