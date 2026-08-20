"""The free NSE layer: shareholding, real filing dates, and XBRL.

None of this needs an API key or a model. It is also the layer where a silent
default does the most damage: a missing pledge read as zero clears exactly the
companies the red flag exists to catch, and a filing date defaulted to the
period end hands a backtest three weeks of free information every quarter.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest

from stockanalysis.db.database import Database
from stockanalysis.ingest.nse_fundamentals import (
    FILING_DATE_ASSUMED,
    FILING_DATE_FROM_NSE,
    apply_results_filing_index,
    parse_financial_results,
    parse_results_comparison,
)
from stockanalysis.ingest.shareholding import (
    assumed_disclosure_date,
    parse_shareholding,
    promoter_holding_trend,
)
from stockanalysis.ingest.xbrl import (
    NotAnnualFiling,
    parse_annual_xbrl,
    parse_xbrl,
    unmapped_facts,
)

ISIN = "INE000000001"
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.upsert_df(
        "instruments",
        pd.DataFrame(
            [
                {
                    "isin": ISIN,
                    "nse_symbol": "TESTCO",
                    "bse_code": None,
                    "name": "Test Company Limited",
                    "sector": "IT",
                    "industry": "IT",
                    "listing_date": dt.date(2010, 1, 1),
                    "delisting_date": None,
                    "is_active": True,
                }
            ]
        ),
        ["isin"],
    )
    yield database
    database.close()


# ----------------------------------------------------------------------
# Shareholding
# ----------------------------------------------------------------------


def test_shareholding_is_parsed_newest_first():
    raw = [
        {"symbol": "TESTCO", "date": "30-Jun-2024", "pr_and_prgrp": "54.2", "public_val": "45.8"},
        {"symbol": "TESTCO", "date": "31-Mar-2024", "pr_and_prgrp": "55.0", "public_val": "45.0"},
    ]
    records = parse_shareholding(raw, ISIN)

    assert [r.quarter_end for r in records] == [dt.date(2024, 6, 30), dt.date(2024, 3, 31)]
    assert records[0].promoter_pct == pytest.approx(54.2)
    assert records[0].public_pct == pytest.approx(45.8)


def test_pledge_is_none_not_zero_when_absent():
    """The trap this guards. NSE's shareholding endpoint carries no pledge
    figure; reading its absence as 0% would give a clean bill of health to
    exactly the companies the >25% pledge red flag exists to catch."""
    raw = [{"date": "31-Mar-2024", "pr_and_prgrp": "55.0"}]
    record = parse_shareholding(raw, ISIN)[0]

    assert record.promoter_pledged_pct is None
    assert record.promoter_pledged_pct != 0


def test_disclosure_date_is_the_lodr_deadline_not_the_quarter_end():
    """The pattern as at 31 March was not public on 31 March."""
    assert assumed_disclosure_date(dt.date(2024, 3, 31)) == dt.date(2024, 4, 21)

    record = parse_shareholding([{"date": "31-Mar-2024"}], ISIN)[0]
    assert record.disclosed_date > record.quarter_end


def test_shareholding_accepts_a_dict_envelope():
    raw = {"data": [{"date": "31-Mar-2024", "pr_and_prgrp": "55.0"}]}
    assert len(parse_shareholding(raw, ISIN)) == 1


def test_shareholding_rows_without_a_date_are_dropped():
    raw = [{"pr_and_prgrp": "55.0"}, {"date": "31-Mar-2024", "pr_and_prgrp": "54.0"}]
    assert len(parse_shareholding(raw, ISIN)) == 1


def test_promoter_trend_respects_the_knowledge_date(db):
    """The red flag reads through disclosed_date, so a backtest cannot see a
    shareholding pattern that had not been filed yet."""
    rows = [
        (dt.date(2024, 6, 30), 52.0),
        (dt.date(2024, 3, 31), 53.0),
        (dt.date(2023, 12, 31), 54.0),
        (dt.date(2023, 9, 30), 55.0),
    ]
    db.upsert_df(
        "shareholding",
        pd.DataFrame(
            [
                {
                    "isin": ISIN,
                    "quarter_end": q,
                    "disclosed_date": assumed_disclosure_date(q),
                    "disclosed_date_source": "ASSUMED_LODR_DEADLINE",
                    "promoter_pct": pct,
                    "promoter_pledged_pct": None,
                    "fii_pct": None,
                    "dii_pct": None,
                    "public_pct": None,
                    "employee_trust_pct": None,
                }
                for q, pct in rows
            ]
        ),
        ["isin", "quarter_end"],
    )

    # After every disclosure: four quarters, strictly falling.
    trend = promoter_holding_trend(db, ISIN, dt.date(2024, 12, 31))
    assert trend == [52.0, 53.0, 54.0, 55.0]
    assert all(trend[i] < trend[i + 1] for i in range(3))

    # The June pattern was filed on 21 July, so on 1 July it is not visible.
    assert promoter_holding_trend(db, ISIN, dt.date(2024, 7, 1)) == [53.0, 54.0, 55.0]


# ----------------------------------------------------------------------
# Real filing dates from the results index
# ----------------------------------------------------------------------


def test_financial_results_index_is_parsed():
    raw = [
        {
            "symbol": "TESTCO",
            "toDate": "31-Mar-2024",
            "broadcastDate": "15-May-2024",
            "relatingTo": "Fourth Quarter",
            "consolidated": "Consolidated",
            "audited": "Audited",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/TESTCO_Q4.xml",
        }
    ]
    f = parse_financial_results(raw)[0]

    assert f.symbol == "TESTCO"
    assert f.period_end == dt.date(2024, 3, 31)
    assert f.broadcast_date == dt.date(2024, 5, 15)
    assert f.is_consolidated is True
    assert f.is_audited is True
    assert f.xbrl_url.endswith("TESTCO_Q4.xml")


def test_impossible_broadcast_date_is_discarded():
    """A broadcast date at or before the period end cannot be when the numbers
    became public. Importing it would be importing lookahead."""
    raw = [{"symbol": "TESTCO", "toDate": "31-Mar-2024", "broadcastDate": "01-Mar-2024"}]
    assert parse_financial_results(raw)[0].broadcast_date is None


def test_consolidated_and_audited_flags_are_tri_state():
    raw = [
        {"symbol": "A", "toDate": "31-Mar-2024", "consolidated": "Non-Consolidated",
         "audited": "Un-Audited"},
        {"symbol": "B", "toDate": "31-Mar-2024"},
    ]
    a, b = parse_financial_results(raw)
    assert a.is_consolidated is False and a.is_audited is False
    # Absent means unknown, not False.
    assert b.is_consolidated is None and b.is_audited is None


def test_real_broadcast_date_replaces_the_assumed_one(db):
    db.upsert_df(
        "fundamentals_quarterly",
        pd.DataFrame(
            [
                {
                    "isin": ISIN,
                    "period_end_date": dt.date(2024, 3, 31),
                    # The SEBI-deadline fallback: 45 days after quarter end.
                    "filing_date": dt.date(2024, 5, 15),
                    "filing_date_source": FILING_DATE_ASSUMED,
                    "revenue": 2500.0,
                    "pat": 375.0,
                    "eps": 12.5,
                    "source": "NSE_RESULTS_COMPARISON",
                }
            ]
        ),
        ["isin", "period_end_date"],
    )

    filings = parse_financial_results(
        [{"symbol": "TESTCO", "toDate": "31-Mar-2024", "broadcastDate": "02-May-2024",
          "xbrl": "https://x.invalid/a.xml"}]
    )
    apply_results_filing_index(db, filings)

    row = db.query("SELECT * FROM fundamentals_quarterly").iloc[0]
    assert pd.Timestamp(row["filing_date"]).date() == dt.date(2024, 5, 2)
    assert row["filing_date_source"] == FILING_DATE_FROM_NSE
    assert row["xbrl_url"] == "https://x.invalid/a.xml"


def _q4(year: int, *, consolidated: bool | None = True, month: int = 3) -> dict:
    """One Q4 entry as NSE's index serves it."""
    end = dt.date(year, month, 30 if month != 3 else 31)
    return {
        "symbol": "TESTCO",
        "toDate": end.strftime("%d-%b-%Y"),
        "broadcastDate": (end + dt.timedelta(days=32)).strftime("%d-%b-%Y"),
        "relatingTo": "Fourth Quarter",
        "consolidated": (
            "Consolidated" if consolidated
            else ("Non-Consolidated" if consolidated is False else "")
        ),
        "audited": "Audited",
        "xbrl": f"https://x.invalid/testco_q4fy{year}.xml",
    }


def test_the_index_is_recorded_for_years_with_no_quarterly_row(db):
    """The bug this closes: `results_comparison` returns only about five
    quarters, so `fundamentals_quarterly` never holds more than one March
    quarter per company. The index itself reaches back years and carries an
    XBRL link on every entry — but an update-only apply could attach those
    links to nothing, so every year before the newest was fetched and dropped,
    and the annual XBRL path could only ever produce a single fiscal year.
    """
    apply_results_filing_index(
        db, parse_financial_results([_q4(2022), _q4(2023), _q4(2024)])
    )

    rows = db.query(
        "SELECT period_end_date, xbrl_url FROM results_filings ORDER BY period_end_date"
    )
    assert len(rows) == 3, "every year in the index should be recorded"
    assert [pd.Timestamp(d).year for d in rows["period_end_date"]] == [2022, 2023, 2024]
    assert rows["xbrl_url"].notna().all()


def test_pending_annual_filings_spans_every_year_the_index_covers(db):
    from stockanalysis.ingest.xbrl_annual import pending_annual_filings

    apply_results_filing_index(
        db, parse_financial_results([_q4(2022), _q4(2023), _q4(2024)])
    )

    refs = pending_annual_filings(db)
    assert sorted(r.fiscal_year for r in refs) == [2022, 2023, 2024]
    assert {r.basis for r in refs} == {"CONSOLIDATED"}
    assert all(r.symbol == "TESTCO" for r in refs)


def test_both_bases_are_recorded_so_consolidated_can_be_preferred(db):
    """`fundamentals_quarterly` is keyed (isin, period_end), so the two filings
    a company makes for one period — consolidated and standalone — collide
    there and one silently overwrites the other. The index table keeps both,
    which is what lets the basis be chosen rather than decided by whichever
    happened to be applied last.
    """
    from stockanalysis.ingest.xbrl_annual import pending_annual_filings

    apply_results_filing_index(
        db,
        parse_financial_results(
            [_q4(2024, consolidated=False), _q4(2024, consolidated=True)]
        ),
    )

    stored = db.query("SELECT basis FROM results_filings ORDER BY basis")
    assert list(stored["basis"]) == ["CONSOLIDATED", "STANDALONE"]

    refs = pending_annual_filings(db)
    assert [r.basis for r in refs] == ["CONSOLIDATED"]


def test_a_company_filing_only_standalone_still_yields_a_filing(db):
    from stockanalysis.ingest.xbrl_annual import pending_annual_filings

    apply_results_filing_index(
        db, parse_financial_results([_q4(2024, consolidated=False)])
    )
    refs = pending_annual_filings(db)
    assert [r.basis for r in refs] == ["STANDALONE"]


def test_unmatched_symbols_leave_rows_untouched(db):
    db.upsert_df(
        "fundamentals_quarterly",
        pd.DataFrame(
            [
                {
                    "isin": ISIN,
                    "period_end_date": dt.date(2024, 3, 31),
                    "filing_date": dt.date(2024, 5, 15),
                    "filing_date_source": FILING_DATE_ASSUMED,
                    "revenue": 2500.0,
                    "pat": 375.0,
                    "eps": 12.5,
                    "source": "NSE_RESULTS_COMPARISON",
                }
            ]
        ),
        ["isin", "period_end_date"],
    )

    apply_results_filing_index(
        db,
        parse_financial_results(
            [{"symbol": "SOMEOTHERCO", "toDate": "31-Mar-2024", "broadcastDate": "02-May-2024"}]
        ),
    )

    row = db.query("SELECT * FROM fundamentals_quarterly").iloc[0]
    assert row["filing_date_source"] == FILING_DATE_ASSUMED


# ----------------------------------------------------------------------
# XBRL
# ----------------------------------------------------------------------


XBRL_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:in-capmkt="http://www.nseindia.com/in-capmkt">
  <xbrli:context id="Q4FY24">
    <xbrli:period>
      <xbrli:startDate>2024-01-01</xbrli:startDate>
      <xbrli:endDate>2024-03-31</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>
  <xbrli:context id="FY24">
    <xbrli:period>
      <xbrli:startDate>2023-04-01</xbrli:startDate>
      <xbrli:endDate>2024-03-31</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>
  <xbrli:context id="Q4FY23">
    <xbrli:period>
      <xbrli:startDate>2023-01-01</xbrli:startDate>
      <xbrli:endDate>2023-03-31</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>

  <in-capmkt:RevenueFromOperations contextRef="Q4FY24">25000000000</in-capmkt:RevenueFromOperations>
  <in-capmkt:ProfitBeforeTax contextRef="Q4FY24">5000000000</in-capmkt:ProfitBeforeTax>
  <in-capmkt:TaxExpense contextRef="Q4FY24">1250000000</in-capmkt:TaxExpense>
  <in-capmkt:ProfitLossForPeriod contextRef="Q4FY24">3750000000</in-capmkt:ProfitLossForPeriod>
  <in-capmkt:BasicEarningsPerShare contextRef="Q4FY24">12.5</in-capmkt:BasicEarningsPerShare>
  <in-capmkt:SomeUnmappedElement contextRef="Q4FY24">99</in-capmkt:SomeUnmappedElement>

  <in-capmkt:RevenueFromOperations contextRef="FY24">98000000000</in-capmkt:RevenueFromOperations>
  <in-capmkt:RevenueFromOperations contextRef="Q4FY23">21000000000</in-capmkt:RevenueFromOperations>
</xbrli:xbrl>
"""


def test_xbrl_facts_are_extracted_and_converted_to_crore():
    facts = parse_xbrl(XBRL_DOC)
    values = facts.to_crore()

    assert facts.period_end == dt.date(2024, 3, 31)
    assert values["revenue"] == pytest.approx(2500.0)  # 25bn rupees == 2,500 crore
    assert values["pat"] == pytest.approx(375.0)


def test_xbrl_picks_the_quarter_not_the_year_to_date():
    """Results filings carry the quarter, the year-to-date and the prior-year
    comparative in one document. Taking facts without checking their context is
    how a parser silently returns the wrong period."""
    facts = parse_xbrl(XBRL_DOC)

    assert facts.period_start == dt.date(2024, 1, 1)  # the quarter, not 2023-04-01
    assert facts.to_crore()["revenue"] == pytest.approx(2500.0)  # not 9,800


def test_xbrl_eps_is_not_scaled():
    assert parse_xbrl(XBRL_DOC).to_crore()["eps_basic"] == pytest.approx(12.5)


def test_xbrl_reports_what_it_ignored():
    """Element naming is the part most likely to need adjustment against real
    filings, so unmapped elements are surfaced rather than dropped silently."""
    assert "someunmappedelement" in unmapped_facts(XBRL_DOC)


def test_xbrl_without_contexts_raises():
    with pytest.raises(ValueError, match="no XBRL contexts"):
        parse_xbrl('<?xml version="1.0"?><root><a>1</a></root>')


# ----------------------------------------------------------------------
# Annual XBRL — the whole statement, not just the P&L
# ----------------------------------------------------------------------


def test_annual_xbrl_carries_the_balance_sheet_and_cash_flow():
    """The claim this module was built on — that results XBRL has no balance
    sheet or cash flow — is false for the Q4/annual filing. Figures below are
    Infosys FY2024 standalone, read off the real filing NSE published.
    """
    facts = parse_annual_xbrl((FIXTURES / "infosys_fy2024_annual.xml").read_bytes())
    v = facts.to_crore()

    assert v["revenue"] == pytest.approx(128_933.0)
    assert v["pat"] == pytest.approx(27_234.0)
    # The two that only the annual report was believed to hold.
    assert v["ocf"] == pytest.approx(20_787.0)
    assert v["capex"] == pytest.approx(1_832.0)
    # Balance sheet, from the instant context.
    assert v["total_assets"] == pytest.approx(114_950.0)
    assert v["total_equity"] == pytest.approx(81_176.0)


def test_annual_xbrl_balance_sheet_identity_holds():
    """Assets == equity + liabilities is the check that catches a parser reading
    the wrong context. It should hold exactly on a real filing."""
    v = parse_annual_xbrl(
        (FIXTURES / "infosys_fy2024_annual.xml").read_bytes()
    ).to_crore()
    assert v["total_assets"] == pytest.approx(v["equity_and_liabilities"], rel=1e-6)


def test_annual_xbrl_prefers_year_to_date_over_the_quarter():
    """Infosys FY2024 declares *identical* dates on its quarter and year-to-date
    contexts — both 2024-01-01 to 2024-03-31 — while `FourD` actually holds the
    full-year figures. A parser trusting the dates returns Q4 revenue (32,001)
    and calls it the year.
    """
    v = parse_annual_xbrl(
        (FIXTURES / "infosys_fy2024_annual.xml").read_bytes()
    ).to_crore()
    assert v["revenue"] == pytest.approx(128_933.0)
    assert v["revenue"] != pytest.approx(32_001.0)


def test_annual_xbrl_reads_the_period_end_from_the_instant_context():
    facts = parse_annual_xbrl((FIXTURES / "infosys_fy2024_annual.xml").read_bytes())
    assert facts.period_end == dt.date(2024, 3, 31)
    assert facts.audited is True


def test_consolidated_pat_is_the_owners_share_not_the_group_total():
    """`ProfitLossForPeriod` is the whole group's profit; `pat` must be the
    parent's share of it, with the minority's share stored beside it.

    L&T FY2024 consolidated, as tagged: 13,059.11 to owners + 2,487.99 to
    non-controlling interests == 15,547.10 for the group. Taking the group total
    as PAT overstates earnings by 19% and inflates every ratio built on it.
    """
    v = parse_annual_xbrl(
        (FIXTURES / "lt_fy2024_consolidated.xml").read_bytes()
    ).to_crore()

    assert v["pat"] == pytest.approx(13_059.11)
    assert v["non_controlling_interest"] == pytest.approx(2_487.99)
    assert v["profit_for_period"] == pytest.approx(15_547.10)
    assert v["pat"] + v["non_controlling_interest"] == pytest.approx(
        v["profit_for_period"]
    )


def test_standalone_pat_falls_back_to_the_group_profit():
    """A standalone filing tags no split — the group's profit is the
    shareholders'. The fallback must fire there and only there."""
    v = parse_annual_xbrl(
        (FIXTURES / "infosys_fy2024_annual.xml").read_bytes()
    ).to_crore()
    assert v["pat"] == pytest.approx(27_234.0)
    assert v["pat"] == pytest.approx(v["profit_for_period"])


def test_a_half_year_filing_is_refused_rather_than_read_as_a_year():
    """Zydus H1 FY25 carries a balance sheet and a cash flow, and its
    year-to-date context is a genuine six months (2024-04-01 to 2024-09-30).
    Everything about it looks annual except the span, and its revenue — 6,781
    crore — would land in `fundamentals_annual` as a plausible full year.

    Unlike Infosys, this filing's dates *do* distinguish the contexts, so they
    are believed and the filing is refused.
    """
    with pytest.raises(NotAnnualFiling):
        parse_annual_xbrl((FIXTURES / "zydus_h1_fy2025.xml").read_bytes())


def test_a_quarterly_filing_is_refused_rather_than_read_as_a_year():
    """XBRL_DOC is a Q4 results filing with no annual context and no balance
    sheet. Returning its quarter as if it were the year would put three months
    of revenue into `fundamentals_annual`, which nothing downstream could catch.
    """
    with pytest.raises(NotAnnualFiling):
        parse_annual_xbrl(XBRL_DOC)


# ----------------------------------------------------------------------
# The annual XBRL ingest
# ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    """Serves one instance, or raises — the two cases the ingest must tell apart."""

    def __init__(self, content: bytes | None = None, error: Exception | None = None):
        self.content, self.error = content, error
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return _FakeResponse(self.content or b"")


@pytest.fixture
def annual_db(db, tmp_path, monkeypatch):
    """A company with one Q4 filing that carries an XBRL link."""
    from stockanalysis.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "request_delay_seconds", 0)
    db.upsert_df(
        "results_filings",
        pd.DataFrame([{
            "isin": ISIN,
            "period_end_date": dt.date(2024, 3, 31),
            "basis": "CONSOLIDATED",
            "broadcast_date": dt.date(2024, 5, 2),
            "relating_to": "Fourth Quarter",
            "is_consolidated": True,
            "is_audited": True,
            "xbrl_url": "https://x.invalid/testco_q4fy24.xml",
        }]),
        ["isin", "period_end_date", "basis"],
    )
    return db


def test_a_filing_with_no_broadcast_date_is_dated_by_the_statutory_deadline(db):
    """NSE occasionally serves an entry with no usable broadcast timestamp.
    Dropping it would cost a company-year outright; the LODR deadline dates it
    conservatively — later than the truth, never earlier.
    """
    from stockanalysis.ingest.xbrl_annual import pending_annual_filings

    entry = _q4(2023)
    del entry["broadcastDate"]
    apply_results_filing_index(db, parse_financial_results([entry]))

    ref = pending_annual_filings(db)[0]
    assert ref.broadcast_date == dt.date(2023, 3, 31) + dt.timedelta(days=60)


def test_an_unreadable_filing_is_not_offered_a_second_time(annual_db):
    """The dead-end this closes: a bank's instance tags no revenue line, so the
    ingest can never write a row from it — but the filing stays on file, so
    `pending_annual_filings` would keep returning it and the Data page would
    keep pointing at a free step that cannot close the gap. Recording the
    attempt is what lets the report fall through to the annual report instead.
    """
    from stockanalysis.ingest.xbrl_annual import (
        ingest_annual_xbrl,
        pending_annual_filings,
    )

    refs = pending_annual_filings(annual_db)
    assert len(refs) == 1

    # A quarterly instance where an annual one was expected: readable, parsed,
    # and structurally unusable — the same shape of failure as a bank's.
    results = ingest_annual_xbrl(
        annual_db, refs, session=_FakeSession(XBRL_DOC.encode())
    )
    assert not results[0].ok

    assert pending_annual_filings(annual_db) == []
    attempt = annual_db.query(
        "SELECT * FROM extraction_attempts WHERE model = 'xbrl'"
    ).iloc[0]
    assert attempt["cost_usd"] == 0.0
    assert "annual" in attempt["error"]

    # And it is a record, not a verdict: asking for everything still returns it,
    # so a parser fix can be re-run over the filings it previously refused.
    assert len(pending_annual_filings(annual_db, only_missing=False)) == 1


def test_a_fetch_failure_leaves_the_filing_pending(annual_db):
    """A rate-limited request says nothing about the filing. Recording it as a
    dead end would retire a company-year over a transient 429."""
    from stockanalysis.ingest.xbrl_annual import (
        ingest_annual_xbrl,
        pending_annual_filings,
    )

    refs = pending_annual_filings(annual_db)
    results = ingest_annual_xbrl(
        annual_db, refs, session=_FakeSession(error=OSError("429 Too Many Requests"))
    )

    assert "fetch failed" in (results[0].error or "")
    assert len(pending_annual_filings(annual_db)) == 1
    assert annual_db.query(
        "SELECT COUNT(*) AS c FROM extraction_attempts"
    )["c"].iloc[0] == 0


# ----------------------------------------------------------------------
# The same quarter, filed twice, in different units
# ----------------------------------------------------------------------

def _quarter(to_dt: str, revenue: float, pat: float, seq: str) -> dict:
    return {
        "re_to_dt": to_dt,
        "re_seq_num": seq,
        "re_res_type": "U",
        "re_total_inc": str(revenue),
        "re_net_profit": str(pat),
    }


# Taken from GAIL's live payload: the December 2024 quarter came back twice,
# exactly 100x apart, with no field in the response declaring either unit.
GAIL_DUPLICATE = {
    "resCmpData": [
        _quarter("31-DEC-2024", 3570747, 386738, "1191981"),      # lakhs
        _quarter("31-DEC-2024", 35707.47, 3867.38, "1191533"),    # crore
        _quarter("30-SEP-2024", 3364421, 267193, "1184209"),
        _quarter("30-JUN-2024", 3406326, 272398, "1178395"),
        _quarter("31-MAR-2024", 3297210, 217697, "1171909"),
    ]
}


def test_a_quarter_filed_twice_in_different_units_yields_one_row():
    """NSE returns duplicates that would violate the (isin, period_end) key.

    The crash is the benign half. The silent half is that both rows are
    multiplied by LAKH_TO_CRORE regardless of which unit they were actually in,
    so keeping the wrong one understates revenue and PAT by 100x — in the very
    table that exists to cross-check the LLM extraction.
    """
    results = parse_results_comparison(GAIL_DUPLICATE, ISIN)

    periods = [r.period_end for r in results]
    assert len(periods) == len(set(periods)) == 4


def test_the_duplicate_matching_the_companys_own_scale_is_kept():
    """Calibrate against the quarters that came back unambiguously.

    GAIL's other three quarters are ~33,000 crore of income. The lakh-
    denominated duplicate converts to 35,707 crore; the crore-denominated one
    converts to 357. Only one of those is the same company.
    """
    results = {r.period_end: r for r in parse_results_comparison(GAIL_DUPLICATE, ISIN)}
    december = results[dt.date(2024, 12, 31)]

    assert december.revenue == pytest.approx(35707.47)
    assert december.pat == pytest.approx(3867.38)

    neighbours = [results[dt.date(2024, 9, 30)].revenue,
                  results[dt.date(2024, 6, 30)].revenue]
    assert 0.5 < december.revenue / max(neighbours) < 2.0


def test_an_uncalibratable_duplicate_is_dropped_rather_than_guessed():
    """With every quarter ambiguous there is no scale to check against.

    Dropping costs one quarter of coverage. Guessing risks a 100x error that
    propagates into growth, margins and the extraction cross-check, and leaves
    no trace once it is in the table.
    """
    payload = {
        "resCmpData": [
            _quarter("31-DEC-2024", 3570747, 386738, "a"),
            _quarter("31-DEC-2024", 35707.47, 3867.38, "b"),
        ]
    }
    assert parse_results_comparison(payload, ISIN) == []
