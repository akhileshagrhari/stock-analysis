"""Per-company data readiness.

Two kinds of test here, and the first kind is the reason the file exists.

`readiness.NEEDS` restates what each factor reads, and a restatement drifts.
The drift guards below fail the build when a factor is added without a
requirement, when a requirement names a column the schema does not have, or when
a dataset points at a pipeline step the runner does not define — the three ways
this module goes quietly wrong.

The second kind checks the claim the module's docstring makes about itself:
coverage measured on a one-company universe equals coverage from the real run.
If that stops being true the report is confidently wrong rather than absent,
which is the failure worth a test.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from stockanalysis.db.database import Database
from stockanalysis.factors.composite import (
    CompositeModel,
    ScoringConfig,
    default_factors,
)
from stockanalysis.factors.panel import PANEL_CACHE
from stockanalysis.serve import readiness as rd
from tests.conftest import (
    make_fundamentals,
    make_instruments,
    make_membership,
    make_prices,
    make_shareholding,
)

AS_OF = dt.date(2023, 12, 1)
INDEX = "TESTIDX"


@pytest.fixture(autouse=True)
def _clear_panel_cache():
    """The panel cache is a module-level global shared by every factor."""
    PANEL_CACHE._key = None
    PANEL_CACHE._panel = None
    yield


def _universe(db: Database, n: int = 8) -> list[str]:
    instruments = make_instruments(n)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    db.upsert_df(
        "index_membership",
        make_membership(isins, INDEX, dt.date(2019, 1, 1)),
        ["index_name", "isin", "from_date"],
    )
    return isins


def _q4_with_an_instance(db: Database, isin: str, period_end: dt.date) -> None:
    """A Q4 results filing carrying a tagged instance — what `xbrl` reads."""
    db.upsert_df(
        "results_filings",
        pd.DataFrame([{
            "isin": isin,
            "period_end_date": period_end,
            "basis": "STANDALONE",
            "broadcast_date": period_end + dt.timedelta(days=50),
            "relating_to": "Fourth Quarter",
            "is_consolidated": False,
            "is_audited": True,
            "xbrl_url": f"https://nsearchives.nseindia.com/corporate/xbrl/{isin}.xml",
        }]),
        ["isin", "period_end_date", "basis"],
    )


def _free_route_exhausted(db: Database, isin: str) -> None:
    """Put a company in the one state where no step can close the annual gap.

    A bank, or a year filed before Ind AS XBRL: the instance is there and the
    parser has read it and found nothing it can use. The refusal on record is
    what makes this different from every other empty `pending_annual_filings` —
    those are filings not yet reached, this is a filing that will never work.
    Since the paid annual-report path was retired, this state has no step behind
    it at all.
    """
    period_end = dt.date(2023, 3, 31)
    _q4_with_an_instance(db, isin, period_end)
    db.upsert_df(
        "extraction_attempts",
        pd.DataFrame([{
            "attempt_id": f"{isin}-FY2023-STANDALONE-xbrl",
            "filing_id": f"https://nsearchives.nseindia.com/corporate/xbrl/{isin}.xml",
            "isin": isin,
            "fiscal_year": 2023,
            "model": "xbrl",
            "run_label": "xbrl-annual",
            "cost_usd": 0.0,
            "confidence": 0.0,
            "error": "no revenue element tagged in this instance",
            "created_at": dt.datetime(2023, 6, 1),
        }]),
        ["attempt_id"],
    )


def _with_prices(db: Database, isins: list[str], end: dt.date = AS_OF) -> None:
    db.upsert_df(
        "prices_daily",
        make_prices(isins, dt.date(2021, 1, 1), end),
        ["isin", "date"],
    )


# ----------------------------------------------------------------------
# Drift guards
# ----------------------------------------------------------------------


def test_every_factor_declares_its_needs():
    declared = set(rd.NEEDS)
    actual = {f.name for f in default_factors()}
    assert actual - declared == set(), (
        "factors with no entry in readiness.NEEDS — their gaps would be "
        "reported as 'no requirement declared'"
    )
    assert declared - actual == set(), "NEEDS entries for factors that no longer exist"


def test_declared_annual_fields_exist_on_the_table(db: Database):
    columns = set(db.query("SELECT * FROM fundamentals_annual LIMIT 0").columns)
    for name, need in rd.NEEDS.items():
        missing = set(need.annual_fields) - columns
        assert not missing, f"{name} declares unknown annual field(s) {missing}"


def test_declared_quarterly_fields_exist_on_the_table(db: Database):
    columns = set(db.query("SELECT * FROM fundamentals_quarterly LIMIT 0").columns)
    for name, need in rd.NEEDS.items():
        missing = set(need.quarterly_fields) - columns
        assert not missing, f"{name} declares unknown quarterly field(s) {missing}"


def test_every_need_names_a_real_dataset():
    for name, need in rd.NEEDS.items():
        unknown = set(need.datasets) - set(rd.DATASETS_BY_KEY)
        assert not unknown, f"{name} needs unknown dataset(s) {unknown}"


def test_every_dataset_points_at_a_real_pipeline_step():
    from stockanalysis.run.steps import STEPS_BY_KEY

    for spec in rd.DATASETS:
        assert spec.step in STEPS_BY_KEY, (
            f"dataset {spec.key} names step {spec.step!r}, which the runner "
            f"does not define — the report would offer a dead end"
        )


def test_flag_datasets_cover_every_reachable_flag():
    from stockanalysis.factors import redflags

    reachable = {d.name for d in redflags.DEFINITIONS if d.reachable}
    assert reachable == set(rd.FLAG_DATASETS), (
        "a reachable red flag with no declared datasets reports UNKNOWN with "
        "nothing an operator can do about it"
    )


# ----------------------------------------------------------------------
# The load-bearing claim
# ----------------------------------------------------------------------


def test_single_company_coverage_matches_the_universe_run(db: Database):
    """Coverage depends only on the company's own data, never on its peers.

    The report computes coverage from a one-company scoring pass because a
    universe pass costs a full panel load. That shortcut is only valid because
    the NaN pattern a factor produces does not depend on who else is in the
    universe, and `sector_zscore` preserves it. Both halves are asserted here
    rather than assumed.
    """
    isins = _universe(db, n=8)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(isins[:4], [2021, 2022, 2023]),
        ["isin", "fiscal_year", "basis"],
    )

    full = CompositeModel().score(db, isins, AS_OF)

    for isin in (isins[0], isins[5]):
        PANEL_CACHE._key = None
        report = rd.readiness(db, isin, AS_OF, index_name=INDEX)
        assert report.coverage == pytest.approx(float(full.coverage[isin]), abs=1e-9)

        computable = {f.name for f in report.factors if f.computable}
        expected = {c for c in full.raw.columns if pd.notna(full.raw.loc[isin, c])}
        assert computable == expected


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def test_bare_instrument_reports_everything_absent(db: Database):
    isins = _universe(db, n=3)

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    assert report.coverage == 0.0
    assert not report.scorable
    # Every source that feeds a factor is a gap. `filings` is deliberately not
    # one: no extractor reads the PDFs since the model-backed path was retired,
    # so an empty `filings` is the resting state rather than something to fix.
    scoring_sources = {s.have for s in report.sources if s.key != "filings"}
    assert scoring_sources == {rd.Have.ABSENT}
    assert next(s for s in report.sources if s.key == "filings").have is rd.Have.PRESENT
    assert report.stored_as_of is None
    # Every gap must name a step, or the report is a complaint rather than a plan.
    assert report.next_steps()


def test_prices_only_covers_exactly_the_momentum_weight(db: Database):
    isins = _universe(db, n=8)
    _with_prices(db, isins)

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    assert report.coverage == pytest.approx(0.15)
    by_family = {f.family: f for f in report.families}
    assert by_family["momentum"].covered == pytest.approx(1.0)
    assert by_family["quality"].covered == 0.0

    prices = next(s for s in report.sources if s.key == "prices")
    assert prices.have is rd.Have.PRESENT
    annual = next(s for s in report.sources if s.key == "annual")
    assert annual.have is rd.Have.ABSENT
    assert "roe" in annual.blocks


def test_stale_prices_are_partial_not_present(db: Database):
    isins = _universe(db, n=3)
    _with_prices(db, isins, end=AS_OF - dt.timedelta(days=120))

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    prices = next(s for s in report.sources if s.key == "prices")
    assert prices.have is rd.Have.PARTIAL
    assert "days before the decision date" in prices.gap


def test_stale_quarterly_is_partial(db: Database):
    isins = _universe(db, n=3)
    db.upsert_df(
        "fundamentals_quarterly",
        pd.DataFrame(
            [
                {
                    "isin": isins[0],
                    "period_end_date": dt.date(2021, 3, 31) + dt.timedelta(days=91 * i),
                    "filing_date": dt.date(2021, 5, 15) + dt.timedelta(days=91 * i),
                    "revenue": 100.0,
                    "pat": 10.0,
                    "eps": 1.0,
                    "source": "NSE",
                }
                for i in range(5)
            ]
        ),
        ["isin", "period_end_date"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    quarterly = next(s for s in report.sources if s.key == "quarterly")
    assert quarterly.have is rd.Have.PARTIAL
    assert "quarters behind" in quarterly.gap


def test_a_downloaded_but_unextracted_filing_is_reported_as_both(db: Database):
    """The PDF is on disk and the financials are still absent. Both are true."""
    isins = _universe(db, n=3)
    _free_route_exhausted(db, isins[0])
    db.upsert_df(
        "filings",
        pd.DataFrame(
            [
                {
                    "filing_id": f"{isins[0]}-2023-AR",
                    "isin": isins[0],
                    "doc_type": "ANNUAL_REPORT",
                    "fiscal_year": 2023,
                    "period_end": dt.date(2023, 3, 31),
                    "broadcast_date": dt.date(2023, 9, 30),
                    "local_path": "data/filings/x/2023.pdf",
                }
            ]
        ),
        ["filing_id"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    filings = next(s for s in report.sources if s.key == "filings")
    annual = next(s for s in report.sources if s.key == "annual")
    assert filings.have is rd.Have.PARTIAL
    assert "0 extracted" in filings.detail
    assert annual.have is rd.Have.ABSENT
    # The annual gap is unreachable here, and an empty step key must not leak
    # into the plan as a step the runner would reject.
    assert annual.step == rd.ANNUAL_UNREACHABLE
    assert "" not in report.next_steps()


def test_a_company_with_no_quarterly_rows_is_sent_down_the_free_route(db: Database):
    """Untried is not the same as exhausted.

    A company nothing has been ingested for yet has no XBRL link for the same
    reason a bank has none — `pending_annual_filings` is empty either way — but
    the free route has not been *attempted* for it, it has merely never been
    reached. Reading that as "XBRL cannot help here" is what sent Tata Motors'
    annual report to Claude at $0.75 a copy on a plan that never so much as
    fetched the filing index.
    """
    isins = _universe(db, n=3)
    _with_prices(db, isins)

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)
    annual = next(s for s in report.sources if s.key == "annual")
    assert annual.step == "results-index"

    # And the plan has to run the free chain in the order that works: the index
    # upgrades quarterly rows that must exist first, and is fetched so the XBRL
    # can be read after it. Nothing here should cost money.
    steps = report.next_steps()
    assert steps.index("quarterly") < steps.index("results-index") < steps.index("xbrl")
    assert "extract" not in steps


def test_the_annual_gap_offers_the_free_step_only_while_it_can_close_it(db: Database):
    """XBRL is free and covers most company-years, so it is what the gap should
    send an operator to. But banks tag no revenue line and pre-Ind-AS years have
    no instance at all — offering a free button that can never close the gap is
    worse than naming the paid step that can.
    """
    isins = _universe(db, n=3)
    _with_prices(db, isins)

    db.upsert_df(
        "results_filings",
        pd.DataFrame([{
            "isin": isins[0],
            "period_end_date": dt.date(2023, 3, 31),
            "basis": "CONSOLIDATED",
            "broadcast_date": dt.date(2023, 5, 20),
            "relating_to": "Fourth Quarter",
            "is_consolidated": True,
            "xbrl_url": "https://nsearchives.nseindia.com/corporate/xbrl/X.xml",
        }]),
        ["isin", "period_end_date", "basis"],
    )

    assert next(
        s for s in rd.readiness(db, isins[0], AS_OF, index_name=INDEX).sources
        if s.key == "annual"
    ).step == "xbrl"


def test_an_unfetched_filing_index_offers_the_fetch_not_the_paid_step(db: Database):
    """Nothing to read is not the same as nothing to be had.

    A company whose quarterly rows still carry the assumed LODR deadline has
    never had the filing index applied, so no XBRL link exists yet — and
    `pending_annual_filings` is empty for exactly the same reason a bank's is.
    Sending an operator to Claude at that point buys a report the exchange
    publishes in machine-readable form for nothing.
    """
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_quarterly",
        pd.DataFrame([{
            "isin": isins[0],
            "period_end_date": dt.date(2023, 3, 31),
            "filing_date": dt.date(2023, 9, 30),
            "filing_date_source": "ASSUMED_LODR_DEADLINE",
            "source": "NSE",
        }]),
        ["isin", "period_end_date"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)
    annual = next(s for s in report.sources if s.key == "annual")
    assert annual.step == "results-index"

    # And fetching the index is only worth doing if the XBRL is read after it.
    steps = report.next_steps()
    assert steps.index("results-index") < steps.index("xbrl")


def test_xbrl_read_to_the_end_of_a_stale_index_does_not_become_a_paid_gap(db: Database):
    """`pending_annual_filings` goes empty on success too, and that is not a
    verdict on what XBRL can reach.

    ABB's FY2023 and FY2024 were read from the exchange's own filings for
    nothing. FY2025 is still missing — not because no instance exists, but
    because the filing index on file stops before it. Reading "nothing pending"
    as "XBRL is finished with this company" offered the paid step for a year the
    exchange publishes free, which is what sent an annual report to Claude
    seconds after the free path had just succeeded twice.
    """
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    _q4_with_an_instance(db, isins[0], dt.date(2023, 3, 31))
    db.upsert_df(
        "fundamentals_annual",
        pd.DataFrame([{
            "isin": isins[0],
            "fiscal_year": 2023,
            "period_end_date": dt.date(2023, 3, 31),
            "filing_date": dt.date(2023, 5, 20),
            "basis": "STANDALONE",
            "source": "XBRL",
            "revenue": 1000.0,
            "pat": 100.0,
        }]),
        ["isin", "fiscal_year", "basis"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)
    annual = next(s for s in report.sources if s.key == "annual")
    assert annual.step != "extract"

    # Refreshing the index is the free way forward, and it can only upgrade
    # quarterly rows that the quarterly ingest has created first.
    steps = report.next_steps()
    assert steps.index("quarterly") < steps.index("results-index") < steps.index("xbrl")
    assert "extract" not in steps


def test_an_exhausted_free_route_names_no_step_at_all(db: Database):
    """Banks and pre-Ind-AS years. The instance has been read and refused, and
    the paid annual-report route that used to be named here is retired — so the
    honest report is a gap with no step, not a button that runs nothing."""
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    _free_route_exhausted(db, isins[0])

    annual = next(
        s for s in rd.readiness(db, isins[0], AS_OF, index_name=INDEX).sources
        if s.key == "annual"
    )
    assert annual.step == rd.ANNUAL_UNREACHABLE
    assert annual.have is not rd.Have.PRESENT


def test_annual_detail_says_which_rows_were_free(db: Database):
    """Provenance is the operator's only way to know whether re-running would
    cost anything, and whether `contingent_liabilities` can ever be filled —
    no XBRL element carries it."""
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_annual",
        pd.DataFrame([
            {
                "isin": isins[0],
                "fiscal_year": 2022,
                "period_end_date": dt.date(2022, 3, 31),
                "filing_date": dt.date(2022, 9, 30),
                "basis": "CONSOLIDATED",
                "source": "LLM",
                "extraction_confidence": 1.0,
            },
            {
                "isin": isins[0],
                "fiscal_year": 2023,
                "period_end_date": dt.date(2023, 3, 31),
                "filing_date": dt.date(2023, 5, 20),
                "basis": "CONSOLIDATED",
                "source": "XBRL",
                "extraction_confidence": 1.0,
            },
        ]),
        ["isin", "fiscal_year"],
    )

    annual = next(
        s for s in rd.readiness(db, isins[0], AS_OF, index_name=INDEX).sources
        if s.key == "annual"
    )
    assert "1 XBRL" in annual.detail
    assert "1 Claude" in annual.detail


def test_present_inputs_that_do_not_define_a_ratio_are_not_a_data_gap(db: Database):
    """A loss-making company is not a company we are missing data for.

    `safe_divide` returns NaN for a negative denominator exactly as it does for
    a missing one. Reporting the first as "run the ingest again" would send an
    operator after data that already exists and will not change the answer.
    """
    isins = _universe(db, n=8)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(
            isins,
            [2021, 2022, 2023],
            overrides={(isins[0], fy): {"total_equity": -500.0} for fy in
                       (2021, 2022, 2023)},
        ),
        ["isin", "fiscal_year", "basis"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    roe = next(f for f in report.factors if f.name == "roe")
    assert not roe.computable
    assert roe.blocked_by == ()
    assert "undefined for this company" in roe.reason

    # And the dataset it reads is still reported as present, because it is.
    annual = next(s for s in report.sources if s.key == "annual")
    assert annual.have is rd.Have.PRESENT
    assert "roe" not in annual.blocks


def test_extracted_but_unpopulated_field_names_the_field(db: Database):
    isins = _universe(db, n=8)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(isins, [2021, 2022, 2023], ocf=None),
        ["isin", "fiscal_year", "basis"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    cfo = next(f for f in report.factors if f.name == "cfo_to_pat")
    assert not cfo.computable
    assert cfo.blocked_by == ("annual",)
    assert "ocf" in cfo.reason


def test_stored_signal_never_comes_from_after_the_decision_date(db: Database):
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    db.upsert_df(
        "signals",
        pd.DataFrame(
            [
                {"isin": isins[0], "as_of_date": AS_OF - dt.timedelta(days=30),
                 "composite_score": 61.0, "signal": "HOLD",
                 "model_version": "old"},
                {"isin": isins[0], "as_of_date": AS_OF + dt.timedelta(days=30),
                 "composite_score": 88.0, "signal": "BUY",
                 "model_version": "future"},
            ]
        ),
        ["isin", "as_of_date"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    assert report.stored_signal == "HOLD"
    assert report.stored_version == "old"
    assert report.stale_signal is True


def test_company_outside_the_index_is_reported_as_unscorable(db: Database):
    isins = _universe(db, n=8)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(isins, [2021, 2022, 2023]),
        ["isin", "fiscal_year", "basis"],
    )
    outsider = make_instruments(9).iloc[8:]
    db.upsert_df("instruments", outsider, ["isin"])
    isin = outsider["isin"].iloc[0]

    report = rd.readiness(db, isin, AS_OF, index_name=INDEX)

    assert report.in_universe is False
    assert report.scorable is False


def test_shareholding_history_clears_the_promoter_flag(db: Database):
    isins = _universe(db, n=3)
    quarters = [dt.date(2022, 12, 31), dt.date(2023, 3, 31),
                dt.date(2023, 6, 30), dt.date(2023, 9, 30)]
    db.upsert_df(
        "shareholding",
        make_shareholding(isins[0], quarters, [50.0, 50.0, 50.0, 50.0]),
        ["isin", "quarter_end"],
    )

    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    flags = {f.name: f for f in report.flags}
    assert flags["promoter_selling"].state == "CLEAR"
    # Unreachable flags stay unknown and must not offer a step that would not help.
    assert flags["promoter_pledge"].state == "UNKNOWN"
    assert flags["promoter_pledge"].blocked_by == ()
    assert flags["promoter_pledge"].reachable is False


def test_resolve_accepts_symbol_or_isin_in_any_case(db: Database):
    isins = _universe(db, n=3)

    assert rd.resolve(db, "test000") == isins[0]
    assert rd.resolve(db, isins[0].lower()) == isins[0]
    assert rd.resolve(db, "NOSUCH") is None


def test_unknown_isin_is_an_error_not_an_empty_report(db: Database):
    _universe(db, n=3)
    with pytest.raises(ValueError, match="not in `instruments`"):
        rd.readiness(db, "INE999999999", AS_OF, index_name=INDEX)


# ----------------------------------------------------------------------
# The --fill plan
# ----------------------------------------------------------------------


def _report_with_an_annual_gap(db: Database) -> rd.Readiness:
    """A gap the paid step is the only route to — the case `--fill` has to price."""
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    _free_route_exhausted(db, isins[0])
    return rd.readiness(db, isins[0], AS_OF, index_name=INDEX)


def test_fill_never_proposes_a_paid_step(db: Database):
    """Nothing the gap report can name costs money any more. A plan that still
    reached a paid step would be spending on annual figures NSE tags for free."""
    from stockanalysis.cli import _fill_steps
    from stockanalysis.run.steps import PAID, STEPS_BY_KEY

    report = _report_with_an_annual_gap(db)
    steps = _fill_steps(report, paid=False)
    assert steps
    assert [k for k in steps if STEPS_BY_KEY[k].cost == PAID] == []
    # Asking for paid explicitly changes nothing, because there is nothing paid.
    assert _fill_steps(report, paid=True) == steps


def test_fill_always_ends_by_re_scoring(db: Database):
    """An ingest that does not re-score leaves the stored signal describing the
    data as it was before the run — the exact confusion this command exists to
    remove."""
    from stockanalysis.cli import _fill_steps

    report = _report_with_an_annual_gap(db)
    assert _fill_steps(report, paid=False)[-1] == "score"
    assert _fill_steps(report, paid=True)[-1] == "score"


def test_fill_crawls_the_index_before_reading_xbrl(db: Database):
    """The crawl is what discovers the instances the XBRL step reads. Reversed,
    the read finds nothing pending and skips, and the gap survives the run."""
    from stockanalysis.cli import _fill_steps

    isins = _universe(db, n=3)
    _with_prices(db, isins)
    report = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)

    steps = _fill_steps(report, paid=False)
    assert steps.index("results-index") < steps.index("xbrl")


def test_fill_plan_is_accepted_by_the_runner(db: Database):
    """Every step the report proposes must be one `company_plan` will take."""
    from stockanalysis.cli import _fill_steps
    from stockanalysis.run.steps import company_plan

    report = _report_with_an_annual_gap(db)
    steps = _fill_steps(report, paid=True)
    plan = company_plan("TEST000", steps)
    assert {s.key for s in plan.steps} == {*steps, "resolve"}


def test_fill_run_persists_a_signal_and_the_report_picks_it_up(db: Database):
    """The whole loop: see the gap, run the steps, read the new evaluation.

    Only the free steps run here — the point under test is that `--fill` writes
    a signal the next readiness call reads back, not that yfinance answers.
    """
    from stockanalysis.cli import _run_fill

    isins = _universe(db, n=8)
    _with_prices(db, isins)
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(isins, [2021, 2022, 2023]),
        ["isin", "fiscal_year", "basis"],
    )

    before = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)
    assert before.stored_as_of is None

    PANEL_CACHE._key = None
    _run_fill(db, "TEST000", ["score"], INDEX, AS_OF, min_coverage=0.5)

    PANEL_CACHE._key = None
    after = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)
    assert after.stored_as_of == AS_OF
    assert after.stale_signal is False
    assert after.stored_signal in {"BUY", "HOLD", "SELL"}


def test_config_floor_decides_scorability(db: Database):
    isins = _universe(db, n=8)
    _with_prices(db, isins)

    strict = rd.readiness(db, isins[0], AS_OF, index_name=INDEX)
    assert strict.coverage == pytest.approx(0.15)
    assert not strict.scorable

    PANEL_CACHE._key = None
    lenient = rd.readiness(
        db, isins[0], AS_OF, index_name=INDEX, config=ScoringConfig(min_coverage=0.1)
    )
    assert lenient.scorable
