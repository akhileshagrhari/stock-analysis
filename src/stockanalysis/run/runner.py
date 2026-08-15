"""Executing a plan — once synchronously, or on a worker thread for the UI.

`execute_plan` is the whole of the orchestration logic and takes a database it
does not own. `JobRunner` adds exactly one thing: a thread, and a database
opened on that thread.

**Why one job at a time.** DuckDB permits a single writer. Two concurrent jobs
would not merely be slow, they would fail halfway through with a lock error
after having already spent money on extraction. The runner therefore refuses a
second start rather than queueing one, so the operator finds out immediately
instead of watching a job sit in a queue.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from collections.abc import Callable

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.run.events import (
    Event,
    JobCancelled,
    JobRecord,
    JobState,
    Reporter,
    StepRecord,
    StepState,
)
from stockanalysis.run.steps import Plan, RunOptions, StepContext, StepSkipped

log = logging.getLogger(__name__)


class JobAlreadyRunning(RuntimeError):
    """Raised when a second job is started while one is still going."""


def new_job(plan: Plan) -> JobRecord:
    """A job record in its pre-run state — every step declared, none started.

    Built before anything runs so the UI can show the full plan, including the
    steps that will be skipped, from the moment the operator hits start.
    """
    return JobRecord(
        job_id=uuid.uuid4().hex[:8],
        title=plan.title,
        steps=[StepRecord(key=s.key, label=s.label) for s in plan.steps],
        context={
            "scope": plan.scope,
            "symbol": plan.symbol,
            "index": plan.options.index_name,
            "as_of": str(plan.options.decision_date()),
        },
    )


def execute_plan(
    db: Database,
    plan: Plan,
    job: JobRecord | None = None,
    on_event: Callable[[Event], None] | None = None,
) -> JobRecord:
    """Run every step in `plan` against `db`, recording what happened.

    A failed step stops the job. That is deliberate and it is not the same
    decision as "a step that had nothing to do": the steps are in dependency
    order, so extraction after a failed download would extract yesterday's
    filings and report a confidence score for stale data. A *skipped* step —
    no API key, nothing pending, NSE has no reports for this company — is not a
    failure and the job carries on.
    """
    job = job or new_job(plan)
    reporter = Reporter(job, None, on_event)

    job.state = JobState.RUNNING
    job.started_at = dt.datetime.now()
    reporter.log(f"{plan.title} — {len(plan.steps)} steps")

    ctx = StepContext(
        db=db,
        report=reporter,
        isins=None if plan.scope == "universe" else [],
        symbol=plan.symbol,
        options=plan.options,
    )

    try:
        for spec in plan.steps:
            job.check_cancelled()
            record = job.step(spec.key)
            ctx.report = reporter.for_step(spec.key)

            record.state = StepState.RUNNING
            record.started_at = dt.datetime.now()
            ctx.report.log(f"▶ {spec.label}")

            try:
                spec.run(ctx)
            except StepSkipped as e:
                record.state = StepState.SKIPPED
                record.message = str(e)
                ctx.report.warn(f"skipped — {e}")
            except JobCancelled:
                record.state = StepState.CANCELLED
                record.finished_at = dt.datetime.now()
                raise
            except Exception as e:  # noqa: BLE001 - any step failure ends the job
                record.state = StepState.FAILED
                record.error = f"{type(e).__name__}: {e}"
                record.finished_at = dt.datetime.now()
                ctx.report.error(f"failed — {record.error}")
                log.exception("step %s failed", spec.key)
                raise
            else:
                record.state = StepState.DONE
                if record.total:
                    record.done = record.total
                ctx.report.log(
                    f"✔ {spec.label} ({record.duration_seconds:.1f}s)"
                )
            finally:
                if record.finished_at is None:
                    record.finished_at = dt.datetime.now()

        job.state = JobState.DONE
        reporter.log(f"Finished in {job.duration_seconds:.1f}s")

    except JobCancelled:
        job.state = JobState.CANCELLED
        reporter.warn("Cancelled. Everything completed before this point is saved.")
        _mark_unreached(job)
    except Exception as e:  # noqa: BLE001
        job.state = JobState.FAILED
        job.error = f"{type(e).__name__}: {e}"
        reporter.error(f"Job failed: {job.error}")
        _mark_unreached(job)
    finally:
        job.finished_at = dt.datetime.now()

    return job


def _mark_unreached(job: JobRecord) -> None:
    """Steps after the failure never ran — say so rather than leaving them pending.

    A pending step next to a finished job reads as "still to come". It is not:
    it will not run unless the job is started again.
    """
    for record in job.steps:
        if record.state == StepState.PENDING:
            record.state = StepState.SKIPPED
            record.message = "not reached — the job stopped before this step"


class JobRunner:
    """Owns the one background job this process is allowed to run.

    The worker thread opens its own `Database`. That is not incidental: DuckDB
    connections are not safe to share across threads, and the writable handle
    has to belong to whichever thread is doing the writing. Everything else in
    the process reads through its own connection — see
    `Database.connect_for_read`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: JobRecord | None = None
        self._thread: threading.Thread | None = None

    @property
    def current(self) -> JobRecord | None:
        return self._job

    def is_active(self) -> bool:
        """True while a job holds — or is about to hold — the write connection."""
        with self._lock:
            if self._job is None:
                return False
            if self._job.is_active:
                return True
            # The record flips to a terminal state inside `execute_plan`, a few
            # instructions before the thread returns and the connection closes.
            # Reporting "idle" in that window would let a reader open read-only
            # against a still-open writable connection, which DuckDB refuses.
            return self._thread is not None and self._thread.is_alive()

    def start(self, plan: Plan, db_path: str | None = None) -> JobRecord:
        with self._lock:
            if self._job is not None and self._job.is_active:
                raise JobAlreadyRunning(
                    f"'{self._job.title}' is still running. DuckDB allows one "
                    f"writer, so jobs run one at a time — cancel it or wait."
                )
            job = new_job(plan)
            self._job = job
            self._thread = threading.Thread(
                target=self._run,
                args=(plan, job, db_path or str(settings.db_path)),
                name=f"job-{job.job_id}",
                daemon=True,
            )
            self._thread.start()
            return job

    def _run(self, plan: Plan, job: JobRecord, db_path: str) -> None:
        try:
            settings.ensure_dirs()
            with Database(db_path) as db:
                execute_plan(db, plan, job)
        except Exception as e:  # noqa: BLE001 - the thread must not die silently
            job.state = JobState.FAILED
            job.error = f"{type(e).__name__}: {e}"
            job.finished_at = dt.datetime.now()
            job.append(
                Event(dt.datetime.now(), None, "error", f"Could not start: {e}")
            )
            _mark_unreached(job)
            log.exception("job %s could not run", job.job_id)

    def cancel(self) -> bool:
        job = self._job
        if job is None or not job.is_active:
            return False
        job.request_cancel()
        return True

    def join(self, timeout: float | None = None) -> None:
        """Wait for the worker. Used by tests and by the CLI, never by the UI."""
        if self._thread is not None:
            self._thread.join(timeout)


# One runner per process. The UI reaches for this; the CLI builds its own.
runner = JobRunner()


def run_now(
    plan: Plan, db: Database | None = None, on_event=None
) -> JobRecord:
    """Run a plan synchronously on the calling thread — the CLI's entry point."""
    if db is not None:
        return execute_plan(db, plan, on_event=on_event)
    settings.ensure_dirs()
    with Database(settings.db_path) as owned:
        return execute_plan(owned, plan, on_event=on_event)


__all__ = [
    "JobAlreadyRunning",
    "JobRunner",
    "RunOptions",
    "execute_plan",
    "new_job",
    "run_now",
    "runner",
]
