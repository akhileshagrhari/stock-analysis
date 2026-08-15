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
    assert {s.have for s in report.sources} == {rd.Have.ABSENT}
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
    # Extraction reads PDFs, so a plan that extracts must also fetch them.
    steps = report.next_steps()
    assert steps.index("filings") < steps.index("extract")


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
    isins = _universe(db, n=3)
    _with_prices(db, isins)
    return rd.readiness(db, isins[0], AS_OF, index_name=INDEX)


def test_fill_drops_paid_steps_unless_asked(db: Database):
    from stockanalysis.cli import _fill_steps

    report = _report_with_an_annual_gap(db)
    assert "extract" in report.next_steps(), "fixture no longer has an annual gap"

    steps = _fill_steps(report, paid=False)
    assert "extract" not in steps
    # And the free steps that were alongside it survive.
    assert "filings" in steps


def test_fill_always_ends_by_re_scoring(db: Database):
    """An ingest that does not re-score leaves the stored signal describing the
    data as it was before the run — the exact confusion this command exists to
    remove."""
    from stockanalysis.cli import _fill_steps

    report = _report_with_an_annual_gap(db)
    assert _fill_steps(report, paid=False)[-1] == "score"
    assert _fill_steps(report, paid=True)[-1] == "score"


def test_fill_fetches_before_it_extracts(db: Database):
    from stockanalysis.cli import _fill_steps

    steps = _fill_steps(_report_with_an_annual_gap(db), paid=True)
    assert steps.index("filings") < steps.index("extract")


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
