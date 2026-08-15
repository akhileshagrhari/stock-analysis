"""Tests for the pipeline runner and its console.

The point of these is that a UI which *reports* a run is only worth having if
the report is true. So the assertions are mostly about honesty: a step that did
nothing must not read as success, a job that failed halfway must not leave later
steps looking pending, and a cancelled job must stop.

Nothing here touches the network. Steps import their ingest functions inside the
function body, which is what lets a test replace them by patching the module
they live in.
"""

from __future__ import annotations

import datetime as dt
import threading

import pandas as pd
import pytest
from conftest import (
    DEFAULT_MOMENTUM_STRENGTH,
    make_instruments,
    make_membership,
    make_prices,
)

from stockanalysis.db.database import Database, SameProcessConfigError
from stockanalysis.run.events import JobRecord, JobState, Reporter, StepRecord, StepState
from stockanalysis.run.runner import JobAlreadyRunning, JobRunner, execute_plan, new_job
from stockanalysis.run.steps import (
    PAID,
    Plan,
    RunOptions,
    StepContext,
    StepSkipped,
    StepSpec,
    available_steps,
    company_plan,
    universe_plan,
)

INDEX = "TESTIDX"
AS_OF = dt.date(2023, 1, 31)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _plan(*specs: StepSpec, scope: str = "company", **options: object) -> Plan:
    return Plan(
        title="test",
        scope=scope,
        steps=specs,
        symbol="TEST001" if scope == "company" else None,
        options=RunOptions(index_name=INDEX, as_of=AS_OF, **options),
    )


def _spec(key: str, fn, **kwargs) -> StepSpec:
    return StepSpec(key=key, label=key.title(), description="", run=fn, **kwargs)


@pytest.fixture
def run_db(tmp_path, monkeypatch):
    """A file-backed database with instruments, prices and membership.

    File-backed because the runner opens its own connection by path — an
    in-memory handle cannot be shared with a worker thread.
    """
    from stockanalysis.config import settings

    path = tmp_path / "run.duckdb"
    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)

    db = Database(path)
    instruments = make_instruments(30)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    db.upsert_df(
        "prices_daily",
        make_prices(isins, start, end, momentum_strength=DEFAULT_MOMENTUM_STRENGTH),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, INDEX, start),
        ["index_name", "isin", "from_date"],
    )
    db.close()

    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return path


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


class TestRegistry:
    def test_step_keys_are_unique(self):
        from stockanalysis.run.steps import STEPS

        keys = [s.key for s in STEPS]
        assert len(keys) == len(set(keys))

    def test_no_paid_step_is_on_by_default(self):
        """Money is never spent because someone accepted the defaults."""
        from stockanalysis.run.steps import STEPS

        assert [s.key for s in STEPS if s.cost == PAID and s.default_on] == []

    def test_company_plan_always_resolves_first(self):
        """Every later step reads `ctx.isins`, which only `resolve` sets."""
        plan = company_plan("RELIANCE", ["score"])
        assert plan.steps[0].key == "resolve"

    def test_company_plan_keeps_registry_order_not_caller_order(self):
        """Dependency order is the registry's, so extraction cannot precede download."""
        plan = company_plan("RELIANCE", ["score", "extract", "prices"])
        assert [s.key for s in plan.steps] == ["resolve", "prices", "extract", "score"]

    def test_unknown_step_is_rejected_with_the_list(self):
        with pytest.raises(ValueError, match="Unknown step"):
            company_plan("RELIANCE", ["definitely-not-a-step"])

    def test_universe_plan_excludes_company_only_steps(self):
        keys = {s.key for s in available_steps("universe")}
        assert "resolve" not in keys and "narrative" not in keys
        assert "seed" in keys

    def test_universe_plan_default_selection_is_free_of_paid_steps(self):
        plan = universe_plan()
        assert all(s.cost != PAID for s in plan.steps)


# ----------------------------------------------------------------------
# Records and reporting
# ----------------------------------------------------------------------


class TestReporting:
    def test_snapshot_is_detached_from_the_running_job(self):
        """The UI iterates a snapshot while the worker appends to the original."""
        job = new_job(_plan(_spec("a", lambda ctx: None)))
        Reporter(job, "a").log("before")
        snapshot = job.snapshot()

        Reporter(job, "a").log("after")
        Reporter(job, "a").summary(rows=5)

        assert [e.message for e in snapshot.events] == ["before"]
        assert len(job.events) == 2
        assert snapshot.step("a").summary == {}     # copied, not aliased
        assert job.step("a").summary == {"rows": 5}

    def test_reporter_writes_to_both_step_and_log(self):
        job = new_job(_plan(_spec("a", lambda ctx: None)))
        report = Reporter(job, "a")
        report.summary(rows=10)
        report.row(fiscal_year=2024, confidence=1.0)
        report.progress(2, 5, "working")
        report.warn("careful")

        record = job.step("a")
        assert record.summary == {"rows": 10}
        assert record.rows == [{"fiscal_year": 2024, "confidence": 1.0}]
        assert (record.done, record.total) == (2, 5)
        assert job.events[-1].level == "warn"

    def test_progress_does_not_write_log_lines(self):
        """A universe crawl calls this hundreds of times; it must not flood the log."""
        job = new_job(_plan(_spec("a", lambda ctx: None)))
        report = Reporter(job, "a")
        for i in range(100):
            report.progress(i, 100)
        assert job.events == []

    def test_as_dict_is_json_safe(self):
        import json

        job = new_job(_plan(_spec("a", lambda ctx: None)))
        job.step("a").state = StepState.DONE
        json.dumps(job.as_dict())   # raises if an enum or a date leaked through


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


class TestExecutePlan:
    def test_steps_run_in_order(self, db: Database):
        seen: list[str] = []
        plan = _plan(
            _spec("a", lambda ctx: seen.append("a")),
            _spec("b", lambda ctx: seen.append("b")),
        )
        job = execute_plan(db, plan)

        assert seen == ["a", "b"]
        assert job.state == JobState.DONE
        assert [s.state for s in job.steps] == [StepState.DONE, StepState.DONE]

    def test_failure_stops_the_job(self, db: Database):
        ran = []

        def boom(ctx):
            raise RuntimeError("NSE said no")

        plan = _plan(_spec("a", boom), _spec("b", lambda ctx: ran.append("b")))
        job = execute_plan(db, plan)

        assert ran == []
        assert job.state == JobState.FAILED
        assert "NSE said no" in job.error
        assert job.step("a").state == StepState.FAILED

    def test_steps_after_a_failure_are_not_left_pending(self, db: Database):
        """Pending next to a dead job reads as "still to come". It is not."""

        def boom(ctx):
            raise RuntimeError("nope")

        job = execute_plan(db, _plan(_spec("a", boom), _spec("b", lambda ctx: None)))

        later = job.step("b")
        assert later.state == StepState.SKIPPED
        assert "not reached" in later.message

    def test_a_skipped_step_does_not_stop_the_job(self, db: Database):
        """No API key is not a failure — the rest of the pipeline still runs."""
        ran = []

        def skip(ctx):
            raise StepSkipped("no API key configured")

        job = execute_plan(
            db, _plan(_spec("a", skip), _spec("b", lambda ctx: ran.append("b")))
        )

        assert ran == ["b"]
        assert job.state == JobState.DONE
        assert job.step("a").state == StepState.SKIPPED
        assert "no API key" in job.step("a").message

    def test_a_skipped_step_never_reads_as_success(self, db: Database):
        def skip(ctx):
            raise StepSkipped("nothing pending")

        job = execute_plan(db, _plan(_spec("a", skip)))
        assert job.step("a").state != StepState.DONE

    def test_cancellation_stops_before_the_next_step(self, db: Database):
        ran = []

        def first(ctx):
            ran.append("a")
            ctx.report.job.request_cancel()

        job = execute_plan(
            db, _plan(_spec("a", first), _spec("b", lambda ctx: ran.append("b")))
        )

        assert ran == ["a"]
        assert job.state == JobState.CANCELLED
        assert job.step("a").state == StepState.DONE   # it did finish
        assert job.step("b").state == StepState.SKIPPED

    def test_cancellation_inside_a_step_marks_that_step_cancelled(self, db: Database):
        def during(ctx):
            ctx.report.job.request_cancel()
            ctx.report.check_cancelled()

        job = execute_plan(db, _plan(_spec("a", during)))

        assert job.state == JobState.CANCELLED
        assert job.step("a").state == StepState.CANCELLED

    def test_every_step_gets_a_duration(self, db: Database):
        job = execute_plan(db, _plan(_spec("a", lambda ctx: None)))
        assert job.step("a").duration_seconds is not None
        assert job.duration_seconds is not None

    def test_on_event_callback_receives_the_log(self, db: Database):
        received = []
        execute_plan(
            db,
            _plan(_spec("a", lambda ctx: ctx.report.log("hello"))),
            on_event=received.append,
        )
        assert any(e.message == "hello" for e in received)


# ----------------------------------------------------------------------
# The real steps
# ----------------------------------------------------------------------


class TestResolveStep:
    def test_resolve_sets_the_isin_for_later_steps(self, seeded_db: Database):
        from stockanalysis.run.steps import _resolve

        job = new_job(_plan(_spec("resolve", _resolve)))
        ctx = StepContext(db=seeded_db, report=Reporter(job, "resolve"), symbol="TEST001")
        _resolve(ctx)

        assert ctx.isins == ["INE000000001"]
        assert ctx.company == "Test Company 1"
        assert job.step("resolve").summary["Symbol"] == "TEST001"

    def test_resolve_accepts_an_isin(self, seeded_db: Database):
        from stockanalysis.run.steps import _resolve

        job = new_job(_plan(_spec("resolve", _resolve)))
        ctx = StepContext(
            db=seeded_db, report=Reporter(job, "resolve"), symbol="INE000000002"
        )
        _resolve(ctx)
        assert ctx.isins == ["INE000000002"]

    def test_unknown_symbol_fails_rather_than_running_on_everything(
        self, seeded_db: Database
    ):
        """A silent fall-through here would run a universe crawl by accident."""
        from stockanalysis.run.steps import _resolve

        job = new_job(_plan(_spec("resolve", _resolve)))
        ctx = StepContext(db=seeded_db, report=Reporter(job, "resolve"), symbol="NOPE")
        with pytest.raises(ValueError, match="not in `instruments`"):
            _resolve(ctx)


class TestPricesStep:
    def test_reports_rows_and_the_range_they_cover(self, seeded_db, monkeypatch):
        """A row count says the call worked; the range says the data is usable."""
        import stockanalysis.ingest.prices as prices_mod
        from stockanalysis.run.steps import _prices

        monkeypatch.setattr(prices_mod, "ingest_prices", lambda *a, **k: 1234)

        job = new_job(_plan(_spec("prices", _prices)))
        ctx = StepContext(
            db=seeded_db,
            report=Reporter(job, "prices"),
            isins=["INE000000001"],
            symbol="TEST001",
        )
        _prices(ctx)

        summary = job.step("prices").summary
        assert summary["price rows stored"] == "1,234"
        assert "→" in summary["covering"]

    def test_stale_prices_are_flagged_not_silently_accepted(
        self, seeded_db, monkeypatch
    ):
        import stockanalysis.ingest.prices as prices_mod
        from stockanalysis.run.steps import _prices

        monkeypatch.setattr(prices_mod, "ingest_prices", lambda *a, **k: 10)

        job = new_job(_plan(_spec("prices", _prices)))
        ctx = StepContext(
            db=seeded_db, report=Reporter(job, "prices"), isins=["INE000000001"]
        )
        _prices(ctx)

        # The fixture's prices stop in 2024; today is well past that.
        assert any(
            e.level == "warn" and "days old" in e.message for e in job.events
        )

    def test_an_empty_fetch_is_a_skip_with_a_reason(self, seeded_db, monkeypatch):
        import stockanalysis.ingest.prices as prices_mod
        from stockanalysis.run.steps import _prices

        monkeypatch.setattr(prices_mod, "ingest_prices", lambda *a, **k: 0)

        job = new_job(_plan(_spec("prices", _prices)))
        ctx = StepContext(
            db=seeded_db, report=Reporter(job, "prices"), isins=["INE000000001"]
        )
        with pytest.raises(StepSkipped, match="yfinance"):
            _prices(ctx)

    def test_progress_hook_names_the_company_in_flight(self, seeded_db):
        """The whole point of the hook: say what is being fetched, not what finished."""
        from stockanalysis.run.steps import _company_progress

        job = new_job(_plan(_spec("prices", lambda ctx: None)))
        ctx = StepContext(db=seeded_db, report=Reporter(job, "prices"))
        hook = _company_progress(ctx, "fetching prices for")
        hook(1, 30, "RELIANCE")

        record = job.step("prices")
        assert record.done == 0 and record.total == 30
        assert "RELIANCE" in record.message


class TestExtractStep:
    """The step whose output is a judgement, so its reporting matters most."""

    @staticmethod
    def _fake_filing(fiscal_year: int = 2024):
        from stockanalysis.extract.pipeline import FilingRow

        return FilingRow(
            filing_id="F1",
            isin="INE000000001",
            symbol="TEST001",
            company="Test Company 1",
            fiscal_year=fiscal_year,
            period_end=dt.date(fiscal_year, 3, 31),
            broadcast_date=dt.date(fiscal_year, 9, 30),
            broadcast_date_source="NSE",
            local_path="/tmp/x.pdf",
        )

    @staticmethod
    def _patch(monkeypatch, results):
        import stockanalysis.extract.factory as factory_mod
        import stockanalysis.extract.pipeline as pipeline_mod

        monkeypatch.setattr(
            pipeline_mod, "pending_filings", lambda *a, **k: [TestExtractStep._fake_filing()]
        )
        monkeypatch.setattr(factory_mod, "make_extractor", lambda model: object())

        def run_extraction(db, filings, extractor, run_label="", progress=None):
            for i, (filing, result, report) in enumerate(results, start=1):
                if progress:
                    progress(i, len(results), filing, result, report)
            return [(r, rep) for _, r, rep in results]

        monkeypatch.setattr(pipeline_mod, "run_extraction", run_extraction)

    def test_confidence_and_verdict_are_reported_per_filing(
        self, seeded_db, monkeypatch
    ):
        from stockanalysis.extract.validate import Check, ValidationReport
        from stockanalysis.run.steps import _extract

        result = _FakeResult(cost=0.42, latency=31.0)
        report = ValidationReport(
            checks=[Check("balance_sheet", True, "HARD", "ok")]
        )
        self._patch(monkeypatch, [(self._fake_filing(), result, report)])

        job = new_job(_plan(_spec("extract", _extract)))
        ctx = StepContext(
            db=seeded_db, report=Reporter(job, "extract"), isins=["INE000000001"]
        )
        _extract(ctx)

        row = job.step("extract").rows[0]
        assert row["confidence"] == 1.0
        assert row["verdict"] == "auto-accept"
        assert row["cost_usd"] == 0.42
        assert job.step("extract").summary["cost"] == "$0.42"

    def test_a_failed_validator_is_named_not_averaged_away(
        self, seeded_db, monkeypatch
    ):
        from stockanalysis.extract.validate import Check, ValidationReport
        from stockanalysis.run.steps import _extract

        report = ValidationReport(
            checks=[Check("accounting_identity", False, "HARD", "assets != L+E by 12%")]
        )
        self._patch(monkeypatch, [(self._fake_filing(), _FakeResult(), report)])

        job = new_job(_plan(_spec("extract", _extract)))
        ctx = StepContext(
            db=seeded_db, report=Reporter(job, "extract"), isins=["INE000000001"]
        )
        _extract(ctx)

        row = job.step("extract").rows[0]
        assert row["confidence"] == 0.0
        assert row["verdict"] == "human review"
        assert "accounting_identity" in row["failed_checks"]
        assert any("assets != L+E" in e.message for e in job.events)
        assert any("review queue" in e.message for e in job.events)

    def test_nothing_pending_is_a_skip_that_says_how_to_rerun(
        self, seeded_db, monkeypatch
    ):
        import stockanalysis.extract.pipeline as pipeline_mod
        from stockanalysis.run.steps import _extract

        monkeypatch.setattr(pipeline_mod, "pending_filings", lambda *a, **k: [])

        job = new_job(_plan(_spec("extract", _extract)))
        ctx = StepContext(
            db=seeded_db, report=Reporter(job, "extract"), isins=["INE000000001"]
        )
        with pytest.raises(StepSkipped, match="re-extract"):
            _extract(ctx)


class TestScoreStep:
    """Runs the real factor model on synthetic data — no network anywhere."""

    def test_scores_the_universe_and_reports_this_company(self, seeded_db: Database):
        from stockanalysis.run.steps import _score

        job = new_job(_plan(_spec("score", _score)))
        ctx = StepContext(
            db=seeded_db,
            report=Reporter(job, "score"),
            isins=["INE000000001"],
            symbol="TEST001",
            options=RunOptions(index_name=INDEX, as_of=AS_OF, min_coverage=0.0),
        )
        _score(ctx)

        summary = job.step("score").summary
        assert summary["universe"] == 30
        assert summary["signals written"] > 0
        assert summary["signal"] in ("BUY", "HOLD", "SELL")

        stored = seeded_db.query(
            "SELECT signal FROM signals WHERE isin = ? AND as_of_date = ?",
            ["INE000000001", AS_OF],
        )
        assert stored["signal"].iloc[0] == summary["signal"]

    def test_family_breakdown_marks_what_was_not_measured(self, seeded_db: Database):
        """An unmeasured family must read as absent, never as average."""
        from stockanalysis.run.steps import _score

        job = new_job(_plan(_spec("score", _score)))
        ctx = StepContext(
            db=seeded_db,
            report=Reporter(job, "score"),
            isins=["INE000000001"],
            symbol="TEST001",
            options=RunOptions(index_name=INDEX, as_of=AS_OF, min_coverage=0.0),
        )
        _score(ctx)

        rows = job.step("score").rows
        assert rows, "no family breakdown reported"
        # The fixture has prices but no fundamentals, so value/quality/growth
        # cannot be computed and must say so.
        unmeasured = [r for r in rows if r["measured"].startswith("no")]
        assert unmeasured and all(r["percentile"] is None for r in unmeasured)

    def test_a_company_outside_the_index_is_reported_not_invented(
        self, seeded_db: Database
    ):
        from stockanalysis.run.steps import _score

        seeded_db.upsert_df(
            "instruments",
            pd.DataFrame(
                [
                    {
                        "isin": "INE999999999",
                        "nse_symbol": "OUTSIDER",
                        "name": "Not In The Index Ltd",
                        "sector": "IT",
                        "is_active": True,
                    }
                ]
            ),
            ["isin"],
        )

        job = new_job(_plan(_spec("score", _score)))
        ctx = StepContext(
            db=seeded_db,
            report=Reporter(job, "score"),
            isins=["INE999999999"],
            symbol="OUTSIDER",
            options=RunOptions(index_name=INDEX, as_of=AS_OF, min_coverage=0.0),
        )
        _score(ctx)

        assert any("was not in the" in e.message for e in job.events)

    def test_unscored_is_reported_as_unscored_not_hold(self, seeded_db: Database):
        from stockanalysis.run.steps import _score

        job = new_job(_plan(_spec("score", _score)))
        ctx = StepContext(
            db=seeded_db,
            report=Reporter(job, "score"),
            isins=["INE000000001"],
            symbol="TEST001",
            # Nothing can clear a 99% coverage floor on price data alone.
            options=RunOptions(index_name=INDEX, as_of=AS_OF, min_coverage=0.99),
        )
        _score(ctx)

        assert job.step("score").summary["signal"] == "unscored"
        assert any("Unscored is not HOLD" in e.message for e in job.events)


# ----------------------------------------------------------------------
# The background runner
# ----------------------------------------------------------------------


class TestJobRunner:
    def test_runs_a_plan_on_a_worker_thread(self, run_db):
        from stockanalysis.run.steps import _resolve, _score

        runner = JobRunner()
        plan = Plan(
            title="Update TEST001",
            scope="company",
            steps=(
                _spec("resolve", _resolve),
                _spec("score", _score),
            ),
            symbol="TEST001",
            options=RunOptions(index_name=INDEX, as_of=AS_OF, min_coverage=0.0),
        )
        runner.start(plan, db_path=str(run_db))
        runner.join(timeout=120)

        job = runner.current
        assert job.state == JobState.DONE, job.error
        assert job.step("score").summary["signal"] in ("BUY", "HOLD", "SELL")

    def test_refuses_a_second_job_rather_than_queueing_it(self, run_db):
        """DuckDB has one writer. Queueing would fail later, after spending money."""
        gate = threading.Event()
        runner = JobRunner()
        plan = _plan(_spec("wait", lambda ctx: gate.wait(30)))

        runner.start(plan, db_path=str(run_db))
        try:
            with pytest.raises(JobAlreadyRunning, match="one at a time"):
                runner.start(plan, db_path=str(run_db))
        finally:
            gate.set()
            runner.join(timeout=30)

    def test_cancel_stops_a_running_job(self, run_db):
        started = threading.Event()

        def slow(ctx):
            started.set()
            for _ in range(200):
                ctx.report.check_cancelled()
                threading.Event().wait(0.05)

        runner = JobRunner()
        runner.start(_plan(_spec("slow", slow)), db_path=str(run_db))
        assert started.wait(30)
        assert runner.cancel()
        runner.join(timeout=30)

        assert runner.current.state == JobState.CANCELLED

    def test_a_thread_level_failure_is_recorded_not_swallowed(self, tmp_path):
        """If the database cannot be opened at all, the job must say so."""
        runner = JobRunner()
        runner.start(
            _plan(_spec("a", lambda ctx: None)),
            db_path=str(tmp_path / "nested" / "absent" / "x.duckdb"),
        )
        runner.join(timeout=30)
        job = runner.current
        # Either it opened (DuckDB creates the file) or it failed loudly — what
        # it must never do is sit in RUNNING forever.
        assert job.state in (JobState.DONE, JobState.FAILED)

    def test_is_active_covers_the_gap_before_the_thread_exits(self, run_db):
        """`is_active` gates read-only opens; a false negative breaks the UI."""
        runner = JobRunner()
        assert not runner.is_active()
        runner.start(_plan(_spec("a", lambda ctx: None)), db_path=str(run_db))
        runner.join(timeout=30)
        assert not runner.is_active()


# ----------------------------------------------------------------------
# Connection sharing — what makes the UI readable during a run
# ----------------------------------------------------------------------


class TestConnectForRead:
    def test_plain_read_only_when_nothing_else_is_open(self, tmp_path):
        path = tmp_path / "a.duckdb"
        Database(path).close()

        db = Database.connect_for_read(path)
        try:
            assert db.read_only is True
        finally:
            db.close()

    def test_joins_a_writable_connection_held_by_this_process(self, tmp_path):
        """This is the case a running job creates, every second, for minutes."""
        path = tmp_path / "b.duckdb"
        writer = Database(path)
        try:
            with pytest.raises(SameProcessConfigError):
                Database(path, read_only=True)

            reader = Database.connect_for_read(path)
            try:
                assert reader.query("SELECT 1 AS x")["x"].iloc[0] == 1
            finally:
                reader.close()
        finally:
            writer.close()

    def test_a_reader_sees_rows_the_worker_just_wrote(self, tmp_path):
        path = tmp_path / "c.duckdb"
        writer = Database(path)
        try:
            writer.upsert_df("instruments", make_instruments(3), ["isin"])
            reader = Database.connect_for_read(path)
            try:
                n = reader.query("SELECT COUNT(*) AS c FROM instruments")["c"].iloc[0]
                assert n == 3
            finally:
                reader.close()
        finally:
            writer.close()


# ----------------------------------------------------------------------
# Console formatting
# ----------------------------------------------------------------------


class TestConsoleHelpers:
    @pytest.fixture(autouse=True)
    def _needs_streamlit(self):
        pytest.importorskip("streamlit")

    def test_format_duration(self):
        from stockanalysis.serve.ops import format_duration

        assert format_duration(None) == "—"
        assert format_duration(9.4) == "9s"
        assert format_duration(75) == "1m 15s"
        assert format_duration(3725) == "1h 02m"

    def test_step_caption_shows_progress_while_running(self):
        from stockanalysis.serve.ops import step_caption

        record = StepRecord(key="prices", label="Prices", state=StepState.RUNNING)
        record.done, record.total, record.message = 3, 30, "fetching TCS"
        assert step_caption(record) == "fetching TCS (3/30)"

    def test_step_caption_gives_the_reason_for_a_skip(self):
        from stockanalysis.serve.ops import step_caption

        record = StepRecord(key="marketaux", label="Marketaux", state=StepState.SKIPPED)
        record.message = "no API key"
        assert step_caption(record) == "no API key"

    def test_step_caption_gives_the_error_for_a_failure(self):
        from stockanalysis.serve.ops import step_caption

        record = StepRecord(key="prices", label="Prices", state=StepState.FAILED)
        record.error = "HTTPError: 429"
        assert step_caption(record) == "HTTPError: 429"

    def test_job_headline_counts_terminal_steps(self):
        from stockanalysis.serve.ops import job_headline

        job = JobRecord(
            job_id="x",
            title="Update RELIANCE",
            steps=[
                StepRecord("a", "A", StepState.DONE),
                StepRecord("b", "B", StepState.SKIPPED),
                StepRecord("c", "C", StepState.PENDING),
            ],
            state=JobState.RUNNING,
        )
        assert "2/3 steps" in job_headline(job)

    def test_events_frame_has_stable_columns_when_empty(self):
        from stockanalysis.serve.ops import events_frame

        frame = events_frame(new_job(_plan(_spec("a", lambda ctx: None))))
        assert list(frame.columns) == ["time", "", "step", "message"]

    def test_symbol_round_trips_through_the_picker_label(self):
        from stockanalysis.serve.ops import instrument_options, symbol_from_option

        df = pd.DataFrame(
            [{"nse_symbol": "M&M", "name": "Mahindra & Mahindra Ltd"}]
        )
        option = instrument_options(df)[0]
        assert symbol_from_option(option) == "M&M"


# ----------------------------------------------------------------------
# The page itself
# ----------------------------------------------------------------------


class TestRunPage:
    """Importing the module proves nothing — page code runs only on render."""

    @pytest.fixture(autouse=True)
    def _needs_streamlit(self):
        pytest.importorskip("streamlit.testing.v1")

    @pytest.fixture(autouse=True)
    def _isolated_runner(self, monkeypatch):
        """A fresh runner per test — the real one is a process-wide singleton."""
        from stockanalysis.serve import ops

        monkeypatch.setattr(ops, "runner", JobRunner())

    def test_starting_a_run_from_the_page_actually_runs_it(self, run_db):
        """The whole point of the page: the button does the work and shows it.

        Everything between the click and the rendered result is exercised here —
        plan construction from the checkboxes, the worker thread, the job
        record, and the finished view.
        """
        from streamlit.testing.v1 import AppTest

        from stockanalysis.serve import dashboard, ops

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        app.sidebar.radio[0].set_value("Run").run()

        # Leave only the two steps that need no network.
        for box in app.checkbox:
            box.set_value(box.label == "Score and persist signal")
        app.text_input(key="index_company").set_value(INDEX)
        app.date_input(key="asof_company").set_value(AS_OF)
        app.slider(key="cov_company").set_value(0.0).run()

        app.button[0].click().run()
        ops.runner.join(timeout=120)

        job = ops.runner.current
        assert job is not None, "the button did not start a job"
        assert job.state == JobState.DONE, job.error
        assert [s.key for s in job.steps] == ["resolve", "score"]
        assert job.step("score").summary["signal"] in ("BUY", "HOLD", "SELL")

        # And the finished job is rendered rather than left invisible.
        app.run()
        assert not app.exception
        assert any("Update TEST" in md.value for md in app.markdown)

    def test_run_page_renders(self, run_db):
        from streamlit.testing.v1 import AppTest

        from stockanalysis.serve import dashboard

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        assert not app.exception, f"render raised: {app.exception}"
        app.sidebar.radio[0].set_value("Run").run()
        assert not app.exception, f"Run page raised: {app.exception}"

    def test_run_page_offers_every_company_step(self, run_db):
        from streamlit.testing.v1 import AppTest

        from stockanalysis.serve import dashboard

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        app.sidebar.radio[0].set_value("Run").run()

        labels = {c.label for c in app.checkbox}
        for spec in available_steps("company"):
            if spec.key == "resolve":
                continue           # fixed, rendered as a lock rather than a box
            assert spec.label in labels

    def test_paid_steps_start_unticked(self, run_db):
        from streamlit.testing.v1 import AppTest

        from stockanalysis.run.steps import STEPS_BY_KEY
        from stockanalysis.serve import dashboard

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        app.sidebar.radio[0].set_value("Run").run()

        paid = {s.label for s in STEPS_BY_KEY.values() if s.cost == PAID}
        rendered = [box for box in app.checkbox if box.label in paid]
        assert len(rendered) == len(paid), "a paid step is missing from the page"
        for box in rendered:
            assert box.value is False, f"{box.label} is on by default"

    def test_universe_scope_warns_about_the_crawl(self, run_db):
        from streamlit.testing.v1 import AppTest

        from stockanalysis.serve import dashboard

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        app.sidebar.radio[0].set_value("Run").run()
        app.radio(key="run_scope").set_value("Whole universe").run()

        assert not app.exception
        assert any("hours" in w.value for w in app.warning)

    def test_empty_database_explains_what_to_do_first(self, tmp_path, monkeypatch):
        from streamlit.testing.v1 import AppTest

        from stockanalysis.config import settings
        from stockanalysis.serve import dashboard

        path = tmp_path / "empty.duckdb"
        Database(path).close()
        monkeypatch.setattr(settings, "db_path", path)

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        app.sidebar.radio[0].set_value("Run").run()

        assert not app.exception
        assert any("seed-universe" in w.value for w in app.warning)


class _FakeResult:
    """Stands in for `ExtractionResult` — the step only reads these four things."""

    def __init__(self, cost: float = 0.5, latency: float = 20.0, error: str | None = None):
        self._cost = cost
        self.latency_seconds = latency
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def cost_usd(self) -> float:
        return self._cost
