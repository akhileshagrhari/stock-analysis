"""What a running job looks like from the outside.

A step reports through `Reporter`; the UI reads `JobRecord.snapshot()`. Those
are the only two contact points, and they are deliberately separated by a lock:
the job runs on a worker thread and Streamlit re-renders on its own thread, so
every read has to be of a stable copy rather than of a structure something else
is appending to.

Records are plain data — dates and floats, no live objects — because the UI must
be able to render a step's outcome long after the step's database handle is
gone.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"       # precondition not met, or nothing to do
    CANCELLED = "cancelled"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATES = {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


@dataclass(frozen=True, slots=True)
class Event:
    """One line in the job log.

    `data` carries whatever the step wanted to show structurally — a confidence
    score, a row count, a cost. The UI may render it; the log stays readable
    without it.
    """

    ts: dt.datetime
    step: str | None
    level: str            # info | warn | error
    message: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(timespec="seconds"),
            "step": self.step,
            "level": self.level,
            "message": self.message,
            **self.data,
        }


@dataclass
class StepRecord:
    """A step's declared identity plus whatever happened when it ran."""

    key: str
    label: str
    state: StepState = StepState.PENDING
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    # Headline numbers, shown as metric tiles: {"rows stored": 1482}.
    summary: dict[str, object] = field(default_factory=dict)
    # Tabular detail, e.g. one row per extracted filing with its confidence.
    rows: list[dict] = field(default_factory=list)
    # Progress within the step, when it knows its own denominator.
    done: int = 0
    total: int = 0
    message: str = ""
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or dt.datetime.now()
        return (end - self.started_at).total_seconds()

    @property
    def is_terminal(self) -> bool:
        return self.state not in (StepState.PENDING, StepState.RUNNING)

    def copy(self) -> StepRecord:
        return replace(
            self,
            summary=dict(self.summary),
            rows=[dict(r) for r in self.rows],
        )


class JobCancelled(Exception):
    """Raised inside a step when the operator asked the job to stop.

    Cancellation is cooperative. It takes effect at the next checkpoint a step
    offers — between filings, between companies — because the underlying ingest
    calls are ordinary blocking functions with no abort channel. A step in the
    middle of a 30-second API call finishes that call first.
    """


@dataclass
class JobRecord:
    """A job's full state. Mutated only by the worker; read only via `snapshot`."""

    job_id: str
    title: str
    steps: list[StepRecord]
    state: JobState = JobState.QUEUED
    created_at: dt.datetime = field(default_factory=dt.datetime.now)
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    events: list[Event] = field(default_factory=list)
    error: str | None = None
    context: dict[str, object] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    # ------------------------------------------------------------------
    # Worker side
    # ------------------------------------------------------------------

    def step(self, key: str) -> StepRecord:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(key)

    def append(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled("cancelled by operator")

    @property
    def is_active(self) -> bool:
        return self.state in (JobState.QUEUED, JobState.RUNNING)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or dt.datetime.now()
        return (end - self.started_at).total_seconds()

    # ------------------------------------------------------------------
    # Reader side
    # ------------------------------------------------------------------

    def snapshot(self) -> JobRecord:
        """A detached copy, safe to render while the worker keeps going.

        Without this the UI can iterate `events` while the worker appends to it,
        which in CPython usually works and occasionally does not. A job's event
        log is small enough that copying it on every rerun is not worth
        optimising.
        """
        with self._lock:
            clone = JobRecord(
                job_id=self.job_id,
                title=self.title,
                steps=[s.copy() for s in self.steps],
                state=self.state,
                created_at=self.created_at,
                started_at=self.started_at,
                finished_at=self.finished_at,
                events=list(self.events),
                error=self.error,
                context=dict(self.context),
            )
            if self._cancel.is_set():
                clone.request_cancel()
            return clone

    def as_dict(self) -> dict:
        """JSON-safe view, for the CLI's --json output and for tests."""
        snap = self.snapshot()
        return {
            "job_id": snap.job_id,
            "title": snap.title,
            "state": snap.state.value,
            "started_at": snap.started_at.isoformat() if snap.started_at else None,
            "duration_seconds": snap.duration_seconds,
            "error": snap.error,
            "context": snap.context,
            "steps": [
                {
                    k: (v.value if isinstance(v, StepState) else v)
                    for k, v in asdict(s).items()
                    if k not in ("started_at", "finished_at")
                }
                | {"duration_seconds": s.duration_seconds}
                for s in snap.steps
            ],
        }


class Reporter:
    """What a step is handed to say what it is doing.

    Every method is safe to call from the worker thread and every one updates
    the step record *and* the job log, so a UI that renders either surface sees
    the same run.
    """

    def __init__(
        self,
        job: JobRecord,
        step_key: str | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.job = job
        self.step_key = step_key
        self._on_event = on_event

    def for_step(self, step_key: str) -> Reporter:
        return Reporter(self.job, step_key, self._on_event)

    # -- logging -------------------------------------------------------

    def log(self, message: str, level: str = "info", **data: object) -> None:
        event = Event(dt.datetime.now(), self.step_key, level, message, dict(data))
        self.job.append(event)
        if self._on_event:
            self._on_event(event)

    def warn(self, message: str, **data: object) -> None:
        self.log(message, "warn", **data)

    def error(self, message: str, **data: object) -> None:
        self.log(message, "error", **data)

    # -- step state ----------------------------------------------------

    def progress(self, done: int, total: int, message: str = "") -> None:
        """Advance the step's own progress bar. Does not write a log line.

        Called once per company or per filing, which for a universe run is
        hundreds of times — logging each one would bury everything else.
        """
        if self.step_key is None:
            return
        with self.job._lock:
            rec = self.job.step(self.step_key)
            rec.done, rec.total = done, total
            if message:
                rec.message = message

    def summary(self, **values: object) -> None:
        """Set the step's headline numbers."""
        if self.step_key is None:
            return
        with self.job._lock:
            self.job.step(self.step_key).summary.update(values)

    def row(self, **values: object) -> None:
        """Append one row of tabular detail to the step."""
        if self.step_key is None:
            return
        with self.job._lock:
            self.job.step(self.step_key).rows.append(dict(values))

    def check_cancelled(self) -> None:
        self.job.check_cancelled()
