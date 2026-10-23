"""Results filed under SEBI's Integrated Filing regime — April 2025 onward.

From the quarter ending March 2025, listed companies file their results as an
"Integrated Filing - Financials" rather than through the old financial-results
module. NSE's two legacy endpoints this project read — `corporates-financial-
results` (the filing index) and `results-comparision` (the quarterly P&L) —
stopped moving at the December 2024 quarter. Measured live on 2026-10-23:
RELIANCE's `results_comparison` still ends at 31-Dec-2024, and the legacy index
returned 3,865 filings for Jan-Mar 2025 against 6 for Jul-Oct 2026.

Every fixture here is trimmed from a real instance on nsearchives, with the
typed-dimension contexts moved ahead of the real ones on purpose — see
`test_typed_dimension_contexts_are_not_the_reporting_period`.
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
    INTEGRATED_FINANCIALS,
    fetch_integrated_filings,
    fiscal_quarter_label,
    fiscal_year_end_months,
    parse_integrated_filings,
    record_results_filings,
)
from stockanalysis.ingest.xbrl import parse_annual_xbrl, parse_xbrl

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RELIANCE_FY2026 = FIXTURES / "reliance_fy2026_integrated.xml"
RELIANCE_Q1_FY2027 = FIXTURES / "reliance_q1_fy2027_integrated.xml"
HDFCBANK_Q1_FY2027 = FIXTURES / "hdfcbank_q1_fy2027_integrated.xml"
INFOSYS_FY2024 = FIXTURES / "infosys_fy2024_annual.xml"

RELIANCE = "INE002A01018"
ABB = "INE117A01022"
HDFCBANK = "INE040A01034"


# ----------------------------------------------------------------------
# Parsing the instance
# ----------------------------------------------------------------------


def test_annual_parser_reads_an_integrated_filing():
    """Same `in-capmkt` element names as the legacy taxonomy, so the annual
    path needs no new mapping — only the context fix below."""
    facts = parse_annual_xbrl(RELIANCE_FY2026.read_bytes())
    v = facts.to_crore()

    assert (facts.period_start, facts.period_end) == (
        dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    assert v["revenue"] == pytest.approx(1_075_675)
    assert v["pat"] == pytest.approx(80_775)  # owners, not the group's 95,754
    assert v["non_controlling_interest"] == pytest.approx(14_979)
    assert v["total_assets"] == pytest.approx(2_178_140)
    assert v["total_equity"] == pytest.approx(1_085_866)
    assert v["ocf"] == pytest.approx(192_113)
    assert facts.audited is True


def test_typed_dimension_contexts_are_not_the_reporting_period():
    """The integrated taxonomy qualifies contexts with `typedMember`, not only
    `explicitMember`. One of them, the OCI breakdown, spans the same twelve
    months as `FourD`; another is an instant on the balance-sheet date.

    The fixture lists both ahead of the real contexts. A parser that admits
    them picks by document order and reads the OCI items as the year's P&L —
    which on the untrimmed filing it only escaped because RELIANCE happened to
    list `FourD` first.
    """
    facts = parse_annual_xbrl(RELIANCE_FY2026.read_bytes())

    assert facts.to_crore()["revenue"] == pytest.approx(1_075_675)
    assert facts.to_crore()["total_assets"] == pytest.approx(2_178_140)


def test_quarterly_parser_reads_the_quarter_not_the_balance_sheet_instant():
    """The integrated instance carries an instant context even for Q1. The old
    parser counted instants as zero-length periods, picked the shortest, and
    returned the balance-sheet date with no P&L at all."""
    facts = parse_xbrl(RELIANCE_Q1_FY2027.read_bytes())
    v = facts.to_crore()

    assert (facts.period_start, facts.period_end) == (
        dt.date(2026, 4, 1), dt.date(2026, 6, 30)
    )
    assert v["revenue"] == pytest.approx(311_850)
    assert v["pat"] == pytest.approx(20_946)
    assert v["eps_basic"] == pytest.approx(15.48)


def test_quarterly_parser_takes_the_quarter_from_a_year_end_filing():
    """The Q4 instance carries the quarter (`OneD`) and the year (`FourD`).
    The quarterly table wants the quarter — the year belongs to the annual
    path, and four times a quarter is not a growth rate."""
    facts = parse_xbrl(RELIANCE_FY2026.read_bytes())
    v = facts.to_crore()

    assert facts.period_start == dt.date(2026, 1, 1)
    assert v["revenue"] == pytest.approx(298_621)
    assert v["pat"] == pytest.approx(16_971)
    assert v["eps_basic"] == pytest.approx(12.54)


def test_quarterly_parser_trusts_the_reported_period_on_legacy_filings():
    """Legacy instances copy the quarter's dates onto `FourD`, so by the
    context alone the two tie. The reported period says `FourD` is the year."""
    facts = parse_xbrl(INFOSYS_FY2024.read_bytes())

    assert facts.period_start == dt.date(2024, 1, 1)
    assert facts.to_crore()["revenue"] == pytest.approx(32_001)  # not 1,28,933


def test_bank_filings_tag_profit_and_eps_under_their_own_names():
    """The banking taxonomy has no `RevenueFromOperations` and names owners'
    profit and EPS differently. HDFC Bank Q1 FY27: 20,383 for the group less
    1,138 to minorities is 19,245 to shareholders."""
    v = parse_xbrl(HDFCBANK_Q1_FY2027.read_bytes()).to_crore()

    assert "revenue" not in v
    assert v["total_income"] == pytest.approx(133_110.36)
    assert v["pat"] == pytest.approx(19_244.71)
    assert v["eps_basic"] == pytest.approx(12.5)


# ----------------------------------------------------------------------
# The filing index
# ----------------------------------------------------------------------


def _entry(symbol: str, qe: str, basis: str, audited: str, broadcast: str | None,
           seq: str, sub: str = "Original", created: str | None = None) -> dict:
    """One row of `integrated-filing-results`, field names as served."""
    return {
        "symbol": symbol,
        "qe_Date": qe,
        "consolidated": basis,
        "audited": audited,
        "broadcast_Date": broadcast,
        "creation_Date": created or broadcast,
        "type": INTEGRATED_FINANCIALS,
        "type_Sub": sub,
        "seq_Id": seq,
        "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/"
                f"INTEGRATED_FILING_INDAS_{seq}_WEB.xml",
    }


@pytest.mark.parametrize(
    ("period_end", "fy_end", "label"),
    [
        (dt.date(2026, 3, 31), 3, "Fourth Quarter"),
        (dt.date(2026, 6, 30), 3, "First Quarter"),
        (dt.date(2025, 12, 31), 3, "Third Quarter"),
        # ABB India runs January to December: its March quarter is its first.
        (dt.date(2026, 3, 31), 12, "First Quarter"),
        (dt.date(2025, 12, 31), 12, "Fourth Quarter"),
        # Siemens runs October to September.
        (dt.date(2025, 9, 30), 9, "Fourth Quarter"),
    ],
)
def test_fiscal_quarter_label(period_end, fy_end, label):
    assert fiscal_quarter_label(period_end, fy_end) == label


def test_integrated_entries_are_normalised_like_legacy_ones():
    raw = {"data": [
        _entry("RELIANCE", "31-MAR-2026", "Consolidated", "Audited",
               "24-Apr-2026 22:57:12", "1658776"),
        _entry("RELIANCE", "30-JUN-2026", "Standalone", "Un-Audited",
               "17-Jul-2026 19:49:04", "1695739"),
    ], "totalCount": 2}

    q4, q1 = parse_integrated_filings(raw)

    assert q4.period_end == dt.date(2026, 3, 31)
    assert q4.broadcast_date == dt.date(2026, 4, 24)
    assert q4.relating_to == "Fourth Quarter"
    assert (q4.is_consolidated, q4.is_audited) == (True, True)
    assert q4.xbrl_url.endswith("INTEGRATED_FILING_INDAS_1658776_WEB.xml")
    assert q1.relating_to == "First Quarter"
    assert (q1.is_consolidated, q1.is_audited) == (False, False)


def test_the_quarter_label_follows_the_company_year_not_the_calendar():
    """The integrated index carries no quarter label, and the audited flag is
    no substitute: TCS, Infosys and Wipro audit every quarter, and ABB's
    un-audited March quarter is the first of its year, not the last."""
    raw = [_entry("ABB", "31-MAR-2026", "Consolidated", "Un-Audited",
                  "08-May-2026 18:07:12", "1664967")]

    (f,) = parse_integrated_filings(raw, fy_end_months={"ABB": 12})

    assert f.relating_to == "First Quarter"


def test_an_original_with_no_broadcast_date_is_dated_by_its_creation():
    """Some originals come back with no broadcast date. The time NSE created
    the entry is when it became public."""
    raw = [_entry("BALPHARMA", "31-MAR-2026", "Consolidated", "Audited", None,
                  "1699144", created="22-Jul-2026 14:24:02")]

    (f,) = parse_integrated_filings(raw)

    assert f.broadcast_date == dt.date(2026, 7, 22)


def test_a_revision_never_replaces_the_original(db_with_universe):
    """L&T's March 2025 quarter: two originals on 9 May 2025, then two
    revisions on 3 July whose XBRL files NSE's own archive answers with a 404.
    A revision winning by recency costs the quarter outright.

    The original is also the point-in-time answer — what the market could read
    on its broadcast date — and every revision has one, so dropping revisions
    loses nothing the index does not already hold.
    """
    raw = [  # newest first, as NSE serves them
        _entry("RELIANCE", "30-JUN-2026", "Consolidated", "Un-Audited", None,
               "2000002", sub="Revision", created="02-Aug-2026 10:00:00"),
        _entry("RELIANCE", "30-JUN-2026", "Consolidated", "Un-Audited",
               "17-Jul-2026 19:50:03", "1695741"),
    ]

    filings = parse_integrated_filings(raw)
    record_results_filings(db_with_universe, filings)

    assert len(filings) == 1
    row = db_with_universe.query("SELECT * FROM results_filings").iloc[0]
    assert pd.Timestamp(row["broadcast_date"]).date() == dt.date(2026, 7, 17)
    assert "1695741" in row["xbrl_url"]


def test_fiscal_year_end_comes_from_the_latest_labelled_year_end(db_with_universe):
    """Latest, not most common: Ambuja and Nestlé both moved from a December
    to a March year end, and their old December filings outnumber the new."""
    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(ABB, dt.date(2023, 12, 31), "Fourth Quarter"),
        _filing(ABB, dt.date(2024, 12, 31), "Fourth Quarter"),
        _filing(RELIANCE, dt.date(2022, 12, 31), "Fourth Quarter"),
        _filing(RELIANCE, dt.date(2023, 12, 31), "Fourth Quarter"),
        _filing(RELIANCE, dt.date(2024, 3, 31), "Fourth Quarter"),
    ]), ["isin", "period_end_date", "basis"])

    months = fiscal_year_end_months(db)

    assert months["ABB"] == 12
    assert months["RELIANCE"] == 3
    assert "HDFCBANK" not in months  # no history: the caller's default applies


def test_an_integrated_year_end_reaches_the_annual_path(db_with_universe):
    """The point of the index: FY2025 onward exists only here, and the annual
    XBRL path finds its work by `relating_to = 'Fourth Quarter'`."""
    from stockanalysis.ingest.xbrl_annual import pending_annual_filings

    record_results_filings(db_with_universe, parse_integrated_filings([
        _entry("RELIANCE", "31-MAR-2026", "Consolidated", "Audited",
               "24-Apr-2026 22:57:12", "1658776"),
        _entry("RELIANCE", "30-JUN-2026", "Consolidated", "Un-Audited",
               "17-Jul-2026 19:50:03", "1695741"),
    ]))

    (ref,) = pending_annual_filings(db_with_universe)
    assert (ref.fiscal_year, ref.basis) == (2026, "CONSOLIDATED")
    assert ref.broadcast_date == dt.date(2026, 4, 24)


class _FakeNse:
    base_url = "https://www.nseindia.com/api"

    def __init__(self, pages: list[dict]):
        self.pages, self.calls = pages, []

    def _req(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        page = self.pages[len(self.calls) - 1]

        class _R:
            def json(self_inner):
                return page

        return _R()


def test_the_integrated_index_is_read_to_the_last_page(monkeypatch):
    from stockanalysis.config import settings

    monkeypatch.setattr(settings, "request_delay_seconds", 0)
    nse = _FakeNse([
        {"data": [{"symbol": "A"}, {"symbol": "B"}], "totalCount": 3},
        {"data": [{"symbol": "C"}], "totalCount": 3},
    ])

    rows = fetch_integrated_filings(nse, dt.date(2026, 7, 1), dt.date(2026, 7, 31))

    assert [r["symbol"] for r in rows] == ["A", "B", "C"]
    url, params = nse.calls[0]
    assert url.endswith("/integrated-filing-results")
    assert params["type"] == INTEGRATED_FINANCIALS
    assert (params["from_date"], params["to_date"]) == ("01-07-2026", "31-07-2026")
    assert [p["page"] for _u, p in nse.calls] == [1, 2]


def test_an_empty_page_ends_the_walk_even_if_the_count_says_otherwise(monkeypatch):
    """`totalCount` is NSE's claim, not a guarantee. Trusting it over an empty
    page is an infinite loop against a rate-limited host."""
    from stockanalysis.config import settings

    monkeypatch.setattr(settings, "request_delay_seconds", 0)
    nse = _FakeNse([{"data": [{"symbol": "A"}], "totalCount": 50}, {"data": []}])

    assert len(fetch_integrated_filings(nse, dt.date(2026, 7, 1),
                                        dt.date(2026, 7, 31))) == 1
    assert len(nse.calls) == 2


# ----------------------------------------------------------------------
# Quarterly figures, from the instances the index points at
# ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeSession:
    """Serves a fixture per URL, keyed by the filing's sequence number."""

    def __init__(self, by_seq: dict[str, bytes]):
        self.by_seq, self.calls = by_seq, 0

    def get(self, url, **kwargs):
        self.calls += 1
        for seq, content in self.by_seq.items():
            if seq in url:
                return _FakeResponse(content)
        raise OSError(f"404 {url}")


def _filing(isin: str, period_end: dt.date, relating_to: str, *,
            consolidated: bool = True, seq: str | None = None,
            broadcast: dt.date | None = None) -> dict:
    seq = seq or f"{isin}{period_end:%Y%m%d}{int(consolidated)}"
    return {
        "isin": isin,
        "period_end_date": period_end,
        "basis": "CONSOLIDATED" if consolidated else "STANDALONE",
        "broadcast_date": broadcast,
        "relating_to": relating_to,
        "is_consolidated": consolidated,
        "is_audited": relating_to == "Fourth Quarter",
        "xbrl_url": f"https://x.invalid/INTEGRATED_FILING_{seq}_WEB.xml",
    }


@pytest.fixture
def db_with_universe(tmp_path, monkeypatch) -> Database:
    from stockanalysis.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "request_delay_seconds", 0)
    db = Database(":memory:")
    db.upsert_df("instruments", pd.DataFrame([
        {"isin": isin, "nse_symbol": sym, "bse_code": None, "name": sym,
         "sector": "X", "industry": "X", "listing_date": dt.date(2000, 1, 1),
         "delisting_date": None, "is_active": True}
        for isin, sym in ((RELIANCE, "RELIANCE"), (ABB, "ABB"),
                          (HDFCBANK, "HDFCBANK"))
    ]), ["isin"])
    return db


def test_quarterly_filings_take_one_basis_per_company(db_with_universe):
    from stockanalysis.ingest.xbrl_quarterly import pending_quarterly_filings

    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(RELIANCE, dt.date(2026, 6, 30), "First Quarter"),
        _filing(RELIANCE, dt.date(2026, 6, 30), "First Quarter", consolidated=False),
        _filing(RELIANCE, dt.date(2026, 3, 31), "Fourth Quarter"),
        _filing(RELIANCE, dt.date(2026, 3, 31), "Fourth Quarter", consolidated=False),
        # Standalone only: a company with no subsidiaries files nothing else.
        _filing(HDFCBANK, dt.date(2026, 6, 30), "First Quarter", consolidated=False),
    ]), ["isin", "period_end_date", "basis"])

    refs = pending_quarterly_filings(db)

    assert {(r.isin, r.period_end, r.basis) for r in refs} == {
        (RELIANCE, dt.date(2026, 6, 30), "CONSOLIDATED"),
        (RELIANCE, dt.date(2026, 3, 31), "CONSOLIDATED"),
        (HDFCBANK, dt.date(2026, 6, 30), "STANDALONE"),
    }


def test_the_basis_is_the_one_that_covers_the_window(db_with_universe):
    """ABB filed standalone only until September 2025, both since. Taking
    consolidated because it exists at all would start the series there, and a
    year-on-year factor would have no base for another year. The standalone
    series is the complete one, so the whole window is read on that basis."""
    from stockanalysis.ingest.xbrl_quarterly import pending_quarterly_filings

    db = db_with_universe
    quarters = [dt.date(2025, 3, 31), dt.date(2025, 6, 30),
                dt.date(2025, 9, 30), dt.date(2025, 12, 31)]
    db.upsert_df("results_filings", pd.DataFrame(
        [_filing(ABB, q, "First Quarter", consolidated=False) for q in quarters]
        + [_filing(ABB, q, "First Quarter") for q in quarters[2:]]
    ), ["isin", "period_end_date", "basis"])

    refs = pending_quarterly_filings(db)

    assert [(r.period_end, r.basis) for r in refs] == [
        (q, "STANDALONE") for q in quarters
    ]


def test_a_quarter_stored_on_the_other_basis_is_read_again(db_with_universe):
    """Once ABB's consolidated filings outnumber the standalone ones in the
    window, the series moves to consolidated. A quarter already read on the
    old basis has to be re-read, or the series ends up half and half."""
    from stockanalysis.ingest.xbrl_quarterly import pending_quarterly_filings

    db = db_with_universe
    q = dt.date(2026, 6, 30)
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(ABB, q, "Second Quarter"),
        _filing(ABB, q, "Second Quarter", consolidated=False),
    ]), ["isin", "period_end_date", "basis"])
    db.upsert_df("fundamentals_quarterly", pd.DataFrame([{
        "isin": ABB, "period_end_date": q, "filing_date": dt.date(2026, 7, 31),
        "revenue": 3_000.0, "pat": 400.0, "source": "XBRL", "is_consolidated": False,
    }]), ["isin", "period_end_date"])

    (ref,) = pending_quarterly_filings(db)

    assert ref.basis == "CONSOLIDATED"


def test_quarterly_filings_respect_the_window(db_with_universe):
    from stockanalysis.ingest.xbrl_quarterly import pending_quarterly_filings

    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(RELIANCE, dt.date(2026, 6, 30), "First Quarter"),
        _filing(RELIANCE, dt.date(2022, 6, 30), "First Quarter"),
    ]), ["isin", "period_end_date", "basis"])

    refs = pending_quarterly_filings(db, since=dt.date(2024, 1, 1))

    assert [r.period_end for r in refs] == [dt.date(2026, 6, 30)]


def test_an_xbrl_quarter_replaces_the_frozen_results_comparison_row(db_with_universe):
    """The legacy row is standalone total income; the XBRL row is consolidated
    revenue from operations — the same definition as the annual table. They
    cannot share a series, so the XBRL row replaces it outright."""
    from stockanalysis.ingest.xbrl_quarterly import (
        ingest_quarterly_xbrl,
        pending_quarterly_filings,
    )

    db = db_with_universe
    db.upsert_df("fundamentals_quarterly", pd.DataFrame([{
        "isin": RELIANCE, "period_end_date": dt.date(2026, 6, 30),
        "filing_date": dt.date(2026, 8, 14), "revenue": 131_474.0, "pat": 8_721.0,
        "eps": 6.4, "source": "NSE_RESULTS_COMPARISON",
        "filing_date_source": FILING_DATE_ASSUMED,
    }]), ["isin", "period_end_date"])
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(RELIANCE, dt.date(2026, 6, 30), "First Quarter", seq="1695741",
                broadcast=dt.date(2026, 7, 17)),
    ]), ["isin", "period_end_date", "basis"])

    session = _FakeSession({"1695741": RELIANCE_Q1_FY2027.read_bytes()})
    (result,) = ingest_quarterly_xbrl(db, pending_quarterly_filings(db), session=session)

    assert result.ok, result.error
    row = db.query("SELECT * FROM fundamentals_quarterly").iloc[0]
    assert row["source"] == "XBRL"
    assert row["revenue"] == pytest.approx(311_850)
    assert row["pat"] == pytest.approx(20_946)
    assert row["eps"] == pytest.approx(15.48)
    assert pd.Timestamp(row["filing_date"]).date() == dt.date(2026, 7, 17)
    assert row["filing_date_source"] == FILING_DATE_FROM_NSE
    assert bool(row["is_consolidated"]) is True

    # Read once; a second run has nothing left to do.
    assert pending_quarterly_filings(db) == []


def test_a_quarter_with_no_broadcast_date_is_dated_by_its_deadline(db_with_universe):
    """Sixty days for a year-end quarter, not forty-five: the annual results
    deadline is the later one, and the earlier would be lookahead."""
    from stockanalysis.ingest.xbrl_quarterly import (
        ingest_quarterly_xbrl,
        pending_quarterly_filings,
    )

    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(RELIANCE, dt.date(2026, 3, 31), "Fourth Quarter", seq="1658776"),
    ]), ["isin", "period_end_date", "basis"])

    session = _FakeSession({"1658776": RELIANCE_FY2026.read_bytes()})
    (result,) = ingest_quarterly_xbrl(db, pending_quarterly_filings(db), session=session)

    assert result.ok, result.error
    row = db.query("SELECT * FROM fundamentals_quarterly").iloc[0]
    assert pd.Timestamp(row["filing_date"]).date() == dt.date(2026, 5, 30)
    assert row["filing_date_source"] == FILING_DATE_ASSUMED
    assert row["revenue"] == pytest.approx(298_621)  # the quarter, not the year


def test_a_bank_quarter_uses_total_income_as_its_top_line(db_with_universe):
    """A bank has no revenue from operations to tag. Interest earned plus
    other income is its top line, and the one the legacy feed carried."""
    from stockanalysis.ingest.xbrl_quarterly import (
        ingest_quarterly_xbrl,
        pending_quarterly_filings,
    )

    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(HDFCBANK, dt.date(2026, 6, 30), "First Quarter", seq="1696123",
                broadcast=dt.date(2026, 7, 18)),
    ]), ["isin", "period_end_date", "basis"])

    session = _FakeSession({"1696123": HDFCBANK_Q1_FY2027.read_bytes()})
    (result,) = ingest_quarterly_xbrl(db, pending_quarterly_filings(db), session=session)

    assert result.ok, result.error
    row = db.query("SELECT * FROM fundamentals_quarterly").iloc[0]
    assert row["revenue"] == pytest.approx(133_110.36)
    assert row["pat"] == pytest.approx(19_244.71)


def test_an_instance_for_another_period_is_not_written(db_with_universe):
    """The index says June; the instance says March. Writing it would file
    one quarter's figures under another's date."""
    from stockanalysis.ingest.xbrl_quarterly import (
        ingest_quarterly_xbrl,
        pending_quarterly_filings,
    )

    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(RELIANCE, dt.date(2026, 6, 30), "First Quarter", seq="9999"),
    ]), ["isin", "period_end_date", "basis"])

    session = _FakeSession({"9999": RELIANCE_FY2026.read_bytes()})
    (result,) = ingest_quarterly_xbrl(db, pending_quarterly_filings(db), session=session)

    assert not result.ok
    assert "2026-03-31" in result.error
    assert db.query("SELECT COUNT(*) AS c FROM fundamentals_quarterly")["c"].iloc[0] == 0


def test_a_failed_fetch_is_reported_and_left_pending(db_with_universe):
    from stockanalysis.ingest.xbrl_quarterly import (
        ingest_quarterly_xbrl,
        pending_quarterly_filings,
    )

    db = db_with_universe
    db.upsert_df("results_filings", pd.DataFrame([
        _filing(RELIANCE, dt.date(2026, 6, 30), "First Quarter", seq="1695741"),
    ]), ["isin", "period_end_date", "basis"])

    (result,) = ingest_quarterly_xbrl(
        db, pending_quarterly_filings(db), session=_FakeSession({})
    )

    assert result.fetch_failed
    assert len(pending_quarterly_filings(db)) == 1
