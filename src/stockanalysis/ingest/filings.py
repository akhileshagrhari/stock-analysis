"""Annual-report acquisition.

`FilingProvider` is the seam, mirroring `PriceProvider`. NseIndiaApi backs it;
BseIndiaApi slots in behind the same interface for BSE-only listings.

RATE DISCIPLINE
---------------
These are unofficial wrappers over a public website. Getting the IP blocked is
the main operational risk of this phase, and it is not recoverable by retrying
harder. So: a delay between every request, exponential backoff on failure, and
an on-disk cache checked *before* the network so a re-run never re-fetches. The
cache is the important one — most re-runs are caused by a bug downstream of the
download, and without it every such bug costs another full crawl.

THE KNOWLEDGE-DATE PROBLEM
--------------------------
`fundamentals_annual.filing_date` is the date the figures became public, and the
backtest may only read rows whose filing_date is on or before the decision date.
NSE's annual-report listing does not reliably carry a broadcast timestamp.

Rather than quietly defaulting to the period end — which would hand the backtest
FY2024 figures in April 2024, four months before anyone had them, and inflate
every result — we fall back to the statutory deadline: an AGM within six months
of the financial year end, so 30 September for a 31 March year end. That is
*late*, deliberately. A knowledge date that is too late costs some signal; one
that is too early manufactures it.

Which of the two was used is recorded in `filings.broadcast_date_source`, so the
difference between data and assumption stays visible instead of becoming folklore.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database

log = logging.getLogger(__name__)

BROADCAST_FROM_NSE = "NSE"
BROADCAST_ASSUMED = "ASSUMED_AGM_DEADLINE"

# Companies Act 2013: AGM within six months of the financial year end.
AGM_DEADLINE_MONTHS = 6

# Keys NseIndiaApi has used for the report URL and the year range. Checked in
# order because the upstream response shape is undocumented and has changed.
_URL_KEYS = ("fileName", "file_name", "attchmntFile", "url")
_TO_YEAR_KEYS = ("toYr", "to_yr", "toYear", "to_year")
_FROM_YEAR_KEYS = ("fromYr", "from_yr", "fromYear", "from_year")
_DATE_KEYS = ("submissionDate", "broadcastDate", "sm_dt", "dt", "an_dt")

_YEAR = re.compile(r"(19|20)\d{2}")


@dataclass(frozen=True)
class AnnualReportRef:
    isin: str
    symbol: str
    fiscal_year: int  # the year the FY *ends* in: FY2024 == year ended 31-Mar-2024
    period_end: dt.date
    source_url: str
    broadcast_date: dt.date
    broadcast_date_source: str

    @property
    def filing_id(self) -> str:
        # Also becomes the Batch API custom_id, so it stays short and free of
        # characters that would need escaping.
        return f"{self.isin}-{self.fiscal_year}-AR"


class FilingProvider(ABC):
    @abstractmethod
    def list_annual_reports(self, symbol: str, isin: str) -> list[AnnualReportRef]: ...

    @abstractmethod
    def download(self, ref: AnnualReportRef, dest: Path) -> Path: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


def _first(record: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = record.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _parse_date(value: str) -> dt.date | None:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(value.strip()[:19], fmt).date()
        except ValueError:
            continue
    return None


def assumed_broadcast_date(period_end: dt.date) -> dt.date:
    """Statutory AGM deadline: period end plus six months."""
    month = period_end.month + AGM_DEADLINE_MONTHS
    year = period_end.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    # 31 March + 6 months lands on 30 September; clamp for other year ends.
    day = min(period_end.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30,
                               31, 31, 30, 31, 30, 31][month - 1])
    return dt.date(year, month, day)


class NseFilingProvider(FilingProvider):
    """NseIndiaApi-backed. `NSE.annual_reports()` is the single most valuable
    call in the project — it is the only free route to the PDFs."""

    def __init__(self, download_dir: Path | None = None, delay: float | None = None):
        self.download_dir = download_dir or (settings.data_dir / "filings" / "_tmp")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay if delay is not None else settings.request_delay_seconds
        self._nse = None

    @property
    def name(self) -> str:
        return "nse"

    def _session(self):
        if self._nse is None:
            from nse import NSE

            self._nse = NSE(download_folder=self.download_dir)
        return self._nse

    def close(self) -> None:
        if self._nse is not None:
            self._nse.exit()
            self._nse = None

    def _call(self, fn, *args):
        """One request, with politeness delay and exponential backoff."""
        last: Exception | None = None
        for attempt in range(settings.max_retries):
            try:
                time.sleep(self.delay)
                return fn(*args)
            except Exception as e:  # noqa: BLE001 - unofficial API, fails many ways
                last = e
                backoff = self.delay * (2**attempt)
                log.warning(
                    "NSE call failed (attempt %d/%d): %s; retrying in %.1fs",
                    attempt + 1, settings.max_retries, e, backoff,
                )
                time.sleep(backoff)
        raise RuntimeError(f"NSE call failed after {settings.max_retries} attempts") from last

    def list_annual_reports(self, symbol: str, isin: str) -> list[AnnualReportRef]:
        raw = self._call(self._session().annual_reports, symbol)
        return parse_annual_reports(raw, symbol=symbol, isin=isin)

    def download(self, ref: AnnualReportRef, dest: Path) -> Path:
        if dest.exists() and dest.stat().st_size > 0:
            log.debug("cached: %s", dest)
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        # download_document handles the zip archives NSE serves for older years.
        tmp = self._call(self._session().download_document, ref.source_url, self.download_dir)
        tmp = Path(tmp)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise FileNotFoundError(f"download produced nothing for {ref.source_url}")
        shutil.move(str(tmp), str(dest))
        return dest


def parse_annual_reports(
    raw: dict, symbol: str, isin: str
) -> list[AnnualReportRef]:
    """Normalise NseIndiaApi's response into refs.

    Kept as a free function, separate from the network call, because the upstream
    shape is undocumented and changes — this is the part that needs tests, and it
    should not need a live NSE session to run them.
    """
    records: list[dict] = []
    for value in (raw or {}).values():
        if isinstance(value, list):
            records.extend(r for r in value if isinstance(r, dict))

    refs: dict[int, AnnualReportRef] = {}
    for rec in records:
        url = _first(rec, _URL_KEYS)
        if not url or not url.lower().startswith("http"):
            continue

        to_year = _first(rec, _TO_YEAR_KEYS)
        fiscal_year: int | None = None
        if to_year and _YEAR.search(to_year):
            fiscal_year = int(_YEAR.search(to_year).group())
        else:
            # Fall back to the last year mentioned in the filename, which for
            # NSE's archive URLs encodes the range: AR_ULTRACEMCO_2010_2011_...
            from_year = _first(rec, _FROM_YEAR_KEYS)
            years = [int(m.group()) for m in _YEAR.finditer(url)]
            if from_year and _YEAR.search(from_year):
                fiscal_year = int(_YEAR.search(from_year).group()) + 1
            elif years:
                fiscal_year = max(years)
        if fiscal_year is None:
            log.warning("cannot determine fiscal year for %s: %s", symbol, url)
            continue

        period_end = dt.date(fiscal_year, 3, 31)

        broadcast = None
        for key in _DATE_KEYS:
            if rec.get(key):
                broadcast = _parse_date(str(rec[key]))
                if broadcast:
                    break

        if broadcast and broadcast > period_end:
            source = BROADCAST_FROM_NSE
        else:
            # A broadcast date on or before the period end is impossible; treat
            # it as noise rather than trusting it into a lookahead bug.
            broadcast = assumed_broadcast_date(period_end)
            source = BROADCAST_ASSUMED

        ref = AnnualReportRef(
            isin=isin,
            symbol=symbol,
            fiscal_year=fiscal_year,
            period_end=period_end,
            source_url=url,
            broadcast_date=broadcast,
            broadcast_date_source=source,
        )
        # A year can be listed more than once (revised filings). Prefer the one
        # with a real broadcast date.
        existing = refs.get(fiscal_year)
        if existing is None or (
            existing.broadcast_date_source == BROADCAST_ASSUMED
            and source == BROADCAST_FROM_NSE
        ):
            refs[fiscal_year] = ref

    return sorted(refs.values(), key=lambda r: r.fiscal_year, reverse=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_annual_reports(
    db: Database,
    isins: list[str] | None = None,
    years: int | None = None,
    provider: FilingProvider | None = None,
    filings_dir: Path | None = None,
) -> int:
    """Download annual reports and register them in `filings`.

    Returns the number of filings registered. Already-downloaded reports are
    re-registered but not re-fetched.
    """
    years = years or settings.filing_years
    provider = provider or NseFilingProvider()
    filings_dir = filings_dir or (settings.data_dir / "filings")

    if isins:
        placeholders = ", ".join("?" for _ in isins)
        df = db.query(
            f"SELECT isin, nse_symbol, name FROM instruments "
            f"WHERE isin IN ({placeholders}) AND nse_symbol IS NOT NULL ORDER BY isin",
            list(isins),
        )
    else:
        df = db.query(
            "SELECT isin, nse_symbol, name FROM instruments "
            "WHERE nse_symbol IS NOT NULL ORDER BY isin"
        )

    registered = 0
    for i, row in enumerate(df.itertuples(index=False), start=1):
        try:
            refs = provider.list_annual_reports(row.nse_symbol, row.isin)
        except Exception as e:  # noqa: BLE001 - one bad symbol must not end the crawl
            log.error("listing failed for %s: %s", row.nse_symbol, e)
            continue

        for ref in refs[:years]:
            dest = filings_dir / ref.isin / f"{ref.fiscal_year}.pdf"
            try:
                path = provider.download(ref, dest)
            except Exception as e:  # noqa: BLE001
                log.error("download failed for %s FY%s: %s", ref.symbol, ref.fiscal_year, e)
                continue

            page_count = _page_count(path)
            db.upsert_df(
                "filings",
                pd.DataFrame(
                    [
                        {
                            "filing_id": ref.filing_id,
                            "isin": ref.isin,
                            "doc_type": "ANNUAL_REPORT",
                            "fiscal_year": ref.fiscal_year,
                            "period_end": ref.period_end,
                            "broadcast_date": ref.broadcast_date,
                            "broadcast_date_source": ref.broadcast_date_source,
                            "source_url": ref.source_url,
                            "local_path": str(path),
                            "sha256": _sha256(path),
                            "page_count": page_count,
                            "bytes": path.stat().st_size,
                        }
                    ]
                ),
                ["filing_id"],
            )
            registered += 1

        if i % 10 == 0:
            log.info("processed %d/%d companies", i, len(df))

    if isinstance(provider, NseFilingProvider):
        provider.close()

    return registered


def _page_count(path: Path) -> int | None:
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception as e:  # noqa: BLE001 - a corrupt PDF is a review case, not a crash
        log.warning("cannot read page count for %s: %s", path, e)
        return None
