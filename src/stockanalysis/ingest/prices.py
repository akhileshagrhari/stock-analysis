"""Price ingestion.

`PriceProvider` is the seam. yfinance backs it in phase 0 because it is free and
needs no key; a broker API (Angel One SmartAPI, Upstox) slots in behind the same
interface when EOD data stops being enough, without touching anything downstream.

CORPORATE ACTIONS ARE NOT OPTIONAL. Splits and bonuses are frequent in Indian
markets. An unadjusted series shows a 1:2 split as a 50% single-day crash, which
a momentum factor happily reads as a real signal. We store raw `close` for
reference and `adj_close` for every return calculation.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database

log = logging.getLogger(__name__)


class PriceProvider(ABC):
    """Fetch daily OHLCV for one instrument. Implementations must return
    adjusted closes, or the whole factor layer is built on sand."""

    @abstractmethod
    def fetch_daily(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        """Returns columns: date, open, high, low, close, adj_close, volume."""

    @property
    @abstractmethod
    def name(self) -> str: ...


class YFinanceProvider(PriceProvider):
    """Free, unofficial, no key. Will break without warning — hence the seam.

    Caches to parquet so a re-run never re-fetches, which is both polite and
    the difference between a 2-second and a 20-minute iteration loop.
    """

    def __init__(self, cache_dir: Path | None = None, delay: float | None = None):
        self.cache_dir = cache_dir or (settings.data_dir / "cache" / "prices")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay if delay is not None else settings.request_delay_seconds

    @property
    def name(self) -> str:
        return "yfinance"

    def _cache_path(self, symbol: str, start: dt.date, end: dt.date) -> Path:
        return self.cache_dir / f"{symbol}_{start:%Y%m%d}_{end:%Y%m%d}.parquet"

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        cache = self._cache_path(symbol, start, end)
        if cache.exists():
            return pd.read_parquet(cache)

        import yfinance as yf

        ticker = f"{symbol}.NS"
        last_err: Exception | None = None
        for attempt in range(settings.max_retries):
            try:
                time.sleep(self.delay)
                raw = yf.Ticker(ticker).history(
                    start=start,
                    end=end + dt.timedelta(days=1),
                    interval="1d",
                    auto_adjust=False,
                    actions=False,
                )
                df = self._normalise(raw)
                if not df.empty:
                    df.to_parquet(cache, index=False)
                return df
            except Exception as e:  # noqa: BLE001 - provider is third-party and flaky
                last_err = e
                backoff = self.delay * (2**attempt)
                log.warning(
                    "fetch failed for %s (attempt %d/%d): %s; retrying in %.1fs",
                    ticker, attempt + 1, settings.max_retries, e, backoff,
                )
                time.sleep(backoff)

        log.error("giving up on %s after %d attempts: %s", ticker, settings.max_retries, last_err)
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "adj_close", "volume"]
        )

    @staticmethod
    def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(
                columns=["date", "open", "high", "low", "close", "adj_close", "volume"]
            )

        df = raw.reset_index()
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

        if "date" not in df.columns:
            for cand in ("index", "datetime"):
                if cand in df.columns:
                    df = df.rename(columns={cand: "date"})
                    break

        # yfinance has flip-flopped on whether auto_adjust=False still yields an
        # "adj_close" column. Fall back to close so we always have the field,
        # and be explicit that this run is then unadjusted.
        if "adj_close" not in df.columns:
            log.warning("no adj_close returned; falling back to raw close (UNADJUSTED)")
            df["adj_close"] = df["close"]

        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.date

        keep = ["date", "open", "high", "low", "close", "adj_close", "volume"]
        for col in keep:
            if col not in df.columns:
                df[col] = None
        return df[keep].dropna(subset=["close"]).reset_index(drop=True)


def ingest_prices(
    db: Database,
    isins: list[str] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    provider: PriceProvider | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """Fetch and persist daily prices for the given instruments.

    `progress` is called as `progress(index, total, symbol)` *before* each
    company is fetched, so a caller can name the request in flight rather than
    the one that just finished. `index` is 1-based. A rate-limited crawl spends
    almost all its time inside the call, which is the part worth showing.
    """
    provider = provider or YFinanceProvider()
    end = end or dt.date.today()
    start = start or (end - dt.timedelta(days=365 * 6))

    if isins:
        placeholders = ", ".join("?" for _ in isins)
        sym_df = db.query(
            f"SELECT isin, nse_symbol FROM instruments "
            f"WHERE isin IN ({placeholders}) AND nse_symbol IS NOT NULL",
            list(isins),
        )
    else:
        sym_df = db.query(
            "SELECT isin, nse_symbol FROM instruments WHERE nse_symbol IS NOT NULL"
        )

    total = 0
    for i, row in enumerate(sym_df.itertuples(index=False), start=1):
        if progress:
            progress(i, len(sym_df), row.nse_symbol)
        df = provider.fetch_daily(row.nse_symbol, start, end)
        if df.empty:
            log.warning("no price data for %s (%s)", row.nse_symbol, row.isin)
            continue
        df = df.copy()
        df["isin"] = row.isin
        df["traded_value"] = df["close"] * df["volume"]
        total += db.upsert_df(
            "prices_daily",
            df[
                [
                    "isin", "date", "open", "high", "low",
                    "close", "adj_close", "volume", "traded_value",
                ]
            ],
            ["isin", "date"],
        )
        if i % 10 == 0:
            log.info("ingested %d/%d instruments", i, len(sym_df))

    return total
