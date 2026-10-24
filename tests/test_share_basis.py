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
import pytest

from stockanalysis.db.database import Database
from stockanalysis.factors.panel import SHARES_IMPLIED, SHARES_STATED, load_panel
from stockanalysis.factors.share_basis import ShareBasis, restate
from stockanalysis.factors.value import BookToPrice, EarningsYield
from stockanalysis.ingest import corporate_actions as ca
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


# ----------------------------------------------------------------------
# Corporate actions from NSE
# ----------------------------------------------------------------------

BAJFINANCE = "INE296A01032"

# Verbatim from NSE's feed, 2026-10-24. BAJFINANCE's bonus and split share an
# ex-date and arrive as two entries — the case Yahoo's own split list gets
# wrong (it reports 2.0 for a tenfold change).
RELIANCE_ACTIONS = [
    {"symbol": "RELIANCE", "subject": " Rights 1:15 @ Premium Rs 1247",
     "exDate": "13-May-2020", "recDate": "14-May-2020"},
    {"symbol": "RELIANCE", "subject": " Dividend - Rs 6.50  Per Share",
     "exDate": "02-Jul-2020", "recDate": "03-Jul-2020"},
    {"symbol": "RELIANCE", "subject": "Demerger", "exDate": "20-Jul-2023",
     "recDate": "20-Jul-2023"},
    {"symbol": "RELIANCE", "subject": "Bonus 1:1", "exDate": "28-Oct-2024",
     "recDate": "28-Oct-2024"},
    {"symbol": "RELIANCE", "subject": "Annual General Meeting", "exDate": "01-Aug-2025",
     "recDate": "-"},
]
BAJFINANCE_ACTIONS = [
    {"symbol": "BAJFINANCE", "subject": "Bonus 4:1", "exDate": "16-Jun-2025",
     "recDate": "16-Jun-2025"},
    {"symbol": "BAJFINANCE",
     "subject": "Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share",
     "exDate": "16-Jun-2025", "recDate": "16-Jun-2025"},
    {"symbol": "BAJFINANCE", "subject": "Special Dividend - Rs 12 Per Share",
     "exDate": "09-May-2025", "recDate": "09-May-2025"},
    {"symbol": "BAJFINANCE", "subject": "Dividend - Rs 44 Per Share",
     "exDate": "09-May-2025", "recDate": "09-May-2025"},
]


class TestParsingNseSubjects:
    def test_bonus_ratio_is_shares_after_over_shares_before(self):
        assert ca.parse_subject("Bonus 1:1") == [(ca.BONUS, 2.0, None)]
        assert ca.parse_subject("Bonus 4:1") == [(ca.BONUS, 5.0, None)]
        assert ca.parse_subject("Bonus 3 : 2") == [(ca.BONUS, 2.5, None)]

    def test_split_ratio_is_old_face_value_over_new(self):
        (split,) = ca.parse_subject(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
        )
        assert split == (ca.SPLIT, 5.0, None)
        (merge,) = ca.parse_subject("Consolidation of shares From Re 1 To Rs 10")
        assert merge == (ca.SPLIT, 0.1, None)

    def test_dividends_on_one_line_add_up(self):
        assert ca.parse_subject(
            "Interim Dividend - Rs 10 Per Share/Special Dividend - Rs.5 Per Share"
        ) == [(ca.DIVIDEND, None, 15.0)]
        assert ca.parse_subject("Annual General Meeting/Dividend-Re 0.50 Per Share") == [
            (ca.DIVIDEND, None, 0.5)
        ]

    def test_rights_and_the_rest(self):
        assert ca.parse_subject("Rights 1:15 @ Premium Rs 1247") == [
            (ca.RIGHTS, 1 / 15, None)
        ]
        assert ca.parse_subject("Demerger") == [(ca.DEMERGER, None, None)]
        assert ca.parse_subject("Annual General Meeting") == []

    def test_a_bonus_of_something_other_than_equity_is_not_a_share_change(self):
        """Read as equity bonuses, these cut EPS by four-fifths and a half for
        companies whose share count never moved."""
        for subject in (
            "Scheme Of Arrangement - Bonus Ncrps 4:1",
            "Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",
            "Bonus Issue Of Preference Shares 1:1",
        ):
            types = [t for t, _r, _a in ca.parse_subject(subject)]
            assert not set(types) & set(ca.SHARE_BASIS_ACTIONS), subject

    def test_a_response_becomes_one_row_per_date_and_type(self):
        actions = ca.parse_actions(BAJFINANCE_ACTIONS, BAJFINANCE)
        by_type = {(a.ex_date, a.action_type): a for a in actions}

        june = dt.date(2025, 6, 16)
        assert by_type[(june, ca.BONUS)].ratio == 5.0
        assert by_type[(june, ca.SPLIT)].ratio == 2.0      # together: tenfold
        dividend = by_type[(dt.date(2025, 5, 9), ca.DIVIDEND)]
        assert dividend.amount == 56.0
        assert "Special Dividend" in dividend.details and "Rs 44" in dividend.details

    def test_dates_and_unparseable_entries(self):
        actions = ca.parse_actions(
            RELIANCE_ACTIONS + [{"subject": "Bonus 1:1", "exDate": "-"}], RELIANCE
        )
        assert [a.action_type for a in actions] == [
            ca.RIGHTS, ca.DIVIDEND, ca.DEMERGER, ca.BONUS
        ]
        assert actions[-1].record_date == dt.date(2024, 10, 28)
        assert actions[-1].details == "Bonus 1:1"


class _FakeNse:
    def __init__(self, responses: dict[str, list[dict] | Exception]):
        self.responses = responses
        self.asked: list[str] = []

    def actions(self, symbol, from_date, to_date):
        self.asked.append(symbol)
        answer = self.responses.get(symbol, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestIngestingCorporateActions:
    def test_actions_are_stored_with_their_source(self, db):
        _instrument(db)
        n = ca.ingest_corporate_actions(
            db, delay=0, nse=_FakeNse({"RELIANCE": RELIANCE_ACTIONS})
        )
        assert n == 4
        bonus = db.query(
            "SELECT * FROM corporate_actions WHERE action_type = 'BONUS'"
        ).iloc[0]
        assert bonus["ratio"] == 2.0 and bonus["source"] == "NSE"
        assert pd.Timestamp(bonus["ex_date"]).date() == dt.date(2024, 10, 28)

    def test_a_refetch_replaces_what_nse_no_longer_lists(self, db):
        _instrument(db)
        ca.ingest_corporate_actions(db, delay=0, nse=_FakeNse({"RELIANCE": RELIANCE_ACTIONS}))
        ca.ingest_corporate_actions(
            db, delay=0, nse=_FakeNse({"RELIANCE": RELIANCE_ACTIONS[-2:]})
        )
        types = db.query("SELECT action_type FROM corporate_actions")["action_type"].tolist()
        assert types == [ca.BONUS]

    def test_an_empty_or_failed_answer_keeps_what_is_stored(self, db):
        """A throttled request looks like a company with no actions. Wiping the
        bonus on that evidence would silently undo the EPS restatement."""
        _instrument(db)
        ca.ingest_corporate_actions(db, delay=0, nse=_FakeNse({"RELIANCE": RELIANCE_ACTIONS}))
        ca.ingest_corporate_actions(db, delay=0, nse=_FakeNse({"RELIANCE": []}))
        ca.ingest_corporate_actions(
            db, delay=0, nse=_FakeNse({"RELIANCE": RuntimeError("429")})
        )
        assert len(db.query("SELECT * FROM corporate_actions")) == 4


def _bars(isin: str, closes: dict[dt.date, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"isin": isin, "date": d, "open": c, "high": c, "low": c, "close": c,
             "adj_close": c, "volume": 1, "traded_value": c}
            for d, c in closes.items()
        ]
    )


class TestPriceSeriesAgainstTheActions:
    def _setup(self, db, before: float, after: float):
        _instrument(db)
        ca.ingest_corporate_actions(db, delay=0, nse=_FakeNse({"RELIANCE": RELIANCE_ACTIONS}))
        db.upsert_df(
            "prices_daily",
            _bars(RELIANCE, {dt.date(2024, 10, 25): before, dt.date(2024, 10, 28): after}),
            ["isin", "date"],
        )

    def test_an_adjusted_series_is_continuous_across_the_bonus(self, db):
        self._setup(db, before=1327.85, after=1334.35)    # what Yahoo serves
        (check,) = ca.price_checks(db)
        assert check.adjusted
        assert ca.unadjusted_in_prices(db) == []

    def test_an_unadjusted_series_jumps_by_the_ratio_and_is_named(self, db):
        self._setup(db, before=2655.70, after=1334.35)
        (seam,) = ca.unadjusted_in_prices(db)
        assert seam.symbol == "RELIANCE" and seam.ex_date == dt.date(2024, 10, 28)
        assert seam.observed == pytest.approx(1.99, abs=0.01)

    def test_a_same_day_bonus_and_split_are_checked_as_one_tenfold_change(self, db):
        _instrument(db, BAJFINANCE, "BAJFINANCE")
        ca.ingest_corporate_actions(
            db, delay=0, nse=_FakeNse({"BAJFINANCE": BAJFINANCE_ACTIONS})
        )
        db.upsert_df(
            "prices_daily",
            _bars(BAJFINANCE, {dt.date(2025, 6, 13): 9331.0, dt.date(2025, 6, 16): 938.0}),
            ["isin", "date"],
        )
        (seam,) = ca.unadjusted_in_prices(db)
        assert seam.ratio == pytest.approx(10.0)


# ----------------------------------------------------------------------
# Restating per-share figures — the D1 arithmetic on RELIANCE's own numbers
# ----------------------------------------------------------------------

BONUS_EX = dt.date(2024, 10, 28)


def _basis(events, price_basis):
    return ShareBasis(events={RELIANCE: events}, price_basis={RELIANCE: price_basis})


class TestTheRestatementFactor:
    def test_a_filing_before_the_bonus_is_restated_by_it(self):
        basis = _basis([(BONUS_EX, 2.0)], dt.date(2026, 8, 14))
        assert basis.factor(RELIANCE, dt.date(2024, 4, 22)) == 2.0     # FY2024
        assert basis.factor(RELIANCE, dt.date(2025, 4, 25)) == 1.0     # FY2025, already post-bonus

    def test_an_action_on_the_filing_date_is_taken_as_already_reflected(self):
        """Ind AS 33 restates EPS for a bonus before the results are approved."""
        basis = _basis([(BONUS_EX, 2.0)], dt.date(2026, 8, 14))
        assert basis.factor(RELIANCE, BONUS_EX) == 1.0

    def test_prices_older_than_the_filing_pull_the_filing_back(self):
        """A series last fetched before the bonus is still in pre-bonus shares,
        so a post-bonus filing is stated back in them — not the other way."""
        basis = _basis([(BONUS_EX, 2.0)], dt.date(2024, 9, 30))
        assert basis.factor(RELIANCE, dt.date(2025, 4, 25)) == 0.5

    def test_a_same_day_bonus_and_split_compound(self):
        basis = _basis([(dt.date(2025, 6, 16), 5.0), (dt.date(2025, 6, 16), 2.0)],
                       dt.date(2026, 8, 14))
        assert basis.factor(RELIANCE, dt.date(2025, 4, 30)) == 10.0

    def test_restate_keeps_what_the_filing_said(self):
        basis = _basis([(BONUS_EX, 2.0)], dt.date(2026, 8, 14))
        frame = pd.DataFrame([{
            "isin": RELIANCE, "filing_date": dt.date(2024, 4, 22), "eps": 102.90,
            "paid_up_capital": 6_766.0, "face_value": 10.0,
        }])
        row = restate(frame, basis).iloc[0]
        assert row["eps"] == pytest.approx(51.45)
        assert row["eps_reported"] == 102.90
        assert row["share_factor"] == 2.0
        assert row["shares_crore"] == pytest.approx(1_353.2)


def _reliance(db, *, annual: dict, quarterly: list[dict] | None = None,
              close: float = 1310.0, adj_close: float | None = None,
              bonus: bool = True) -> None:
    """RELIANCE as the 16 Aug 2026 run saw it: prices on the post-bonus basis
    Yahoo serves, and fundamentals as each filing reported them."""
    _instrument(db)
    days = pd.bdate_range(dt.date(2024, 1, 1), dt.date(2026, 8, 14))
    db.upsert_df("prices_daily", pd.DataFrame({
        "isin": RELIANCE, "date": [d.date() for d in days], "open": close,
        "high": close, "low": close, "close": close,
        "adj_close": close if adj_close is None else adj_close,
        "volume": 1, "traded_value": close,
    }), ["isin", "date"])
    db.upsert_df("fundamentals_annual", pd.DataFrame([{
        "isin": RELIANCE, "basis": "CONSOLIDATED", "source": "XBRL", **annual,
    }]), ["isin", "fiscal_year", "basis"])
    if quarterly:
        db.upsert_df("fundamentals_quarterly", pd.DataFrame(
            [{"isin": RELIANCE, **q} for q in quarterly]
        ), ["isin", "period_end_date"])
    if bonus:
        ca.ingest_corporate_actions(
            db, delay=0, nse=_FakeNse({"RELIANCE": RELIANCE_ACTIONS})
        )


FY2024 = {
    "fiscal_year": 2024, "period_end_date": dt.date(2024, 3, 31),
    "filing_date": dt.date(2024, 4, 22), "pat": 69_621.0, "eps": 102.90,
    "total_equity": 925_788.0,
}


class TestValueFactorsOnOneBasis:
    AS_OF = dt.date(2026, 8, 14)

    def test_earnings_yield_divides_like_for_like(self, db):
        """The spec's arithmetic: 0.07855 was exactly 102.90 / 1,310. On one
        basis it is half that."""
        _reliance(db, annual=FY2024)
        panel = load_panel(db, [RELIANCE], self.AS_OF)
        ey = EarningsYield().from_panel(panel)[RELIANCE]
        assert ey == pytest.approx(102.90 / 2 / 1310.0)
        assert ey != pytest.approx(0.07855, rel=1e-3)

    def test_book_to_price_uses_the_restated_share_count(self, db):
        """1.0445 was 9,25,788 crore over 676.6 crore pre-bonus shares at ₹1,310."""
        _reliance(db, annual=FY2024)
        panel = load_panel(db, [RELIANCE], self.AS_OF)
        assert panel.share_source[RELIANCE] == SHARES_IMPLIED
        assert panel.shares[RELIANCE] / 1e7 == pytest.approx(1_353.18, rel=1e-4)
        bp = BookToPrice().from_panel(panel)[RELIANCE]
        assert bp == pytest.approx(1.0445 / 2, rel=1e-3)

    def test_without_the_action_on_file_nothing_is_restated(self, db):
        """What the 16 Aug run did — and the number it stored."""
        _reliance(db, annual=FY2024, bonus=False)
        panel = load_panel(db, [RELIANCE], self.AS_OF)
        assert EarningsYield().from_panel(panel)[RELIANCE] == pytest.approx(0.07855, rel=1e-3)

    def test_the_stated_share_count_wins_and_is_restated_too(self, db):
        """Q2 FY2025 was filed two weeks before the bonus, at 676.6 crore shares."""
        _reliance(db, annual=FY2024, quarterly=[{
            "period_end_date": dt.date(2024, 9, 30), "filing_date": dt.date(2024, 10, 14),
            "revenue": 231_535.0, "pat": 16_563.0, "eps": 24.48,
            "paid_up_capital": 6_766.0, "face_value": 10.0,
        }])
        panel = load_panel(db, [RELIANCE], dt.date(2024, 12, 2))
        assert panel.share_source[RELIANCE] == SHARES_STATED
        assert panel.shares[RELIANCE] / 1e7 == pytest.approx(1_353.2)
        assert panel.quarterly.iloc[0]["eps"] == pytest.approx(12.24)

    def test_a_loss_maker_with_a_stated_count_gets_a_market_cap(self, db):
        """PAT / EPS is undefined on a loss; the filing's own count is not."""
        _reliance(db, bonus=False, annual={
            **FY2024, "pat": -500.0, "eps": -0.74,
            "paid_up_capital": 13_532.0, "face_value": 10.0,
        })
        panel = load_panel(db, [RELIANCE], self.AS_OF)
        assert panel.market_cap[RELIANCE] == pytest.approx(1310.0 * 1_353.2)
        assert BookToPrice().from_panel(panel)[RELIANCE] > 0

    def test_valuation_reads_the_close_not_the_dividend_adjusted_close(self, db):
        _reliance(db, annual=FY2024, close=1310.0, adj_close=1200.0)
        panel = load_panel(db, [RELIANCE], self.AS_OF)
        assert panel.price[RELIANCE] == 1310.0


# ----------------------------------------------------------------------
# D1a / D2: a one-company job must not score everyone else on stale prices
# silently
# ----------------------------------------------------------------------


class TestStalePrices:
    """The 24 Oct 2026 run fetched one company's prices and scored 78."""

    def _universe(self, db, last_day: dt.date):
        from conftest import make_instruments, make_membership, make_prices

        instruments = make_instruments(10)
        db.upsert_df("instruments", instruments, ["isin"])
        isins = instruments["isin"].tolist()
        db.upsert_df(
            "prices_daily", make_prices(isins, dt.date(2022, 1, 3), last_day), ["isin", "date"]
        )
        db.upsert_df(
            "index_membership", make_membership(isins, "TESTIDX", dt.date(2020, 1, 1)),
            ["index_name", "isin", "from_date"],
        )
        return isins

    def test_trend_and_relative_strength_refuse_a_stale_close(self, db):
        """The value factors already refused it; these read it as today's."""
        from stockanalysis.factors.momentum import PriceTo200DMA, RelativeStrength

        isins = self._universe(db, last_day=dt.date(2026, 8, 11))
        fresh = dt.date(2026, 8, 14)
        stale = dt.date(2026, 10, 24)                  # 74 days on
        for factor in (PriceTo200DMA(), RelativeStrength()):
            assert factor.compute(db, isins, fresh).notna().all(), factor.name
            assert factor.compute(db, isins, stale).isna().all(), factor.name

    def test_the_score_step_says_how_many_were_scored_on_stale_prices(self, db):
        from stockanalysis.run.events import Reporter
        from stockanalysis.run.runner import new_job
        from stockanalysis.run.steps import (
            STEPS_BY_KEY,
            Plan,
            RunOptions,
            StepContext,
            _score,
        )

        isins = self._universe(db, last_day=dt.date(2026, 8, 11))
        options = RunOptions(index_name="TESTIDX", as_of=dt.date(2026, 10, 24), min_coverage=0.0)
        job = new_job(Plan(title="t", scope="company", steps=(STEPS_BY_KEY["score"],),
                           symbol="TEST001", options=options))
        ctx = StepContext(db=db, report=Reporter(job, "score"), isins=[isins[1]],
                          symbol="TEST001", options=options)
        _score(ctx)

        assert job.step("score").summary["stale prices"] == "10/10"
        warning = next(e.message for e in job.events if "latest price" in e.message)
        assert "10 of 10" in warning and "TEST001's inputs only" in warning
