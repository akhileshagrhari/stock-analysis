"""Prices and per-share fundamentals on one share basis — data issues D1 and D4.

RELIANCE's 2024 value factors divided a pre-bonus EPS by a bonus-adjusted
price and came out twice as cheap as the stock was. These tests hold the pieces
that prevent that: what the stored closes are, how a fetch keeps one company's
series on one basis, and (further down) how a filing's per-share figures are
restated onto that basis.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.ingest.prices import PriceProvider, ingest_prices

RELIANCE = "INE002A01018"


class _RecordingProvider(PriceProvider):
    """Returns one flat bar per business day and remembers what was asked for."""

    def __init__(self, close: float = 100.0):
        self.close = close
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    @property
    def name(self) -> str:
        return "recording"

    def fetch_daily(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        days = pd.bdate_range(start, end)
        return pd.DataFrame(
            {
                "date": [d.date() for d in days],
                "open": self.close,
                "high": self.close,
                "low": self.close,
                "close": self.close,
                "adj_close": self.close,
                "volume": 1000,
            }
        )


def _instrument(db: Database, isin: str = RELIANCE, symbol: str = "RELIANCE") -> None:
    db.upsert_df(
        "instruments",
        pd.DataFrame([{"isin": isin, "nse_symbol": symbol, "name": symbol}]),
        ["isin"],
    )


class TestOneFetchOneBasis:
    def test_first_fetch_uses_the_requested_window(self, db):
        _instrument(db)
        provider = _RecordingProvider()
        ingest_prices(db, start=dt.date(2024, 1, 1), end=dt.date(2024, 3, 1),
                      provider=provider)
        assert provider.calls == [("RELIANCE", dt.date(2024, 1, 1), dt.date(2024, 3, 1))]

    def test_refetch_reaches_back_to_the_earliest_stored_day(self, db):
        """A split between two fetches re-bases Yahoo's whole series. Rows the
        second fetch did not cover would stay on the old basis, half or a tenth
        of their neighbours, so the second fetch must cover them all."""
        _instrument(db)
        first = _RecordingProvider(close=2000.0)
        ingest_prices(db, start=dt.date(2020, 8, 14), end=dt.date(2024, 10, 1),
                      provider=first)

        # The bonus: Yahoo now reports the same history at half the price.
        second = _RecordingProvider(close=1000.0)
        ingest_prices(db, start=dt.date(2020, 10, 25), end=dt.date(2024, 11, 1),
                      provider=second)

        assert second.calls[0][1] == dt.date(2020, 8, 14)
        closes = db.query(
            "SELECT DISTINCT close FROM prices_daily WHERE isin = ?", [RELIANCE]
        )["close"].tolist()
        assert closes == [1000.0], "the stored series mixes two share bases"

    def test_an_earlier_requested_start_still_wins(self, db):
        _instrument(db)
        ingest_prices(db, start=dt.date(2022, 1, 3), end=dt.date(2022, 2, 1),
                      provider=_RecordingProvider())
        provider = _RecordingProvider()
        ingest_prices(db, start=dt.date(2021, 1, 4), end=dt.date(2022, 3, 1),
                      provider=provider)
        assert provider.calls[0][1] == dt.date(2021, 1, 4)


class TestTheClosesAreDocumentedAsWhatTheyAre:
    def test_schema_no_longer_calls_close_the_traded_price(self):
        from stockanalysis.db.database import SCHEMA_PATH

        schema = SCHEMA_PATH.read_text()
        assert "raw traded price" not in schema
        assert "NOT the traded price" in schema
