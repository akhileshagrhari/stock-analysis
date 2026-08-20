"""The run console — starting a pipeline and watching it work.

This is the operator half of the dashboard. Every other page reads what the
pipeline produced; this one runs it and shows each step as it happens: which
endpoint is being hit, how many rows landed, what confidence each extracted
report came back with.

Two things it deliberately does not do:

- **It does not run the work in the Streamlit script thread.** A price ingest
  takes minutes and a universe crawl takes hours; doing that inline would block
  the browser and lose everything on a refresh. Work happens on `run.runner`'s
  worker thread and this page renders a snapshot of it.
- **It does not decide anything about the pipeline.** The steps, their order,
  their cost and their defaults come from `run.steps`, so the console and the
  CLI cannot drift apart.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.run import steps as step_registry
from stockanalysis.run.events import JobRecord, JobState, StepRecord, StepState
from stockanalysis.run.runner import JobAlreadyRunning, runner
from stockanalysis.run.steps import FREE, NETWORK, PAID, RunOptions

STATE_ICON = {
    StepState.PENDING: "⚪",
    StepState.RUNNING: "🔄",
    StepState.DONE: "✅",
    StepState.FAILED: "❌",
    StepState.SKIPPED: "⏭️",
    StepState.CANCELLED: "⏹️",
}

JOB_ICON = {
    JobState.QUEUED: "⏳",
    JobState.RUNNING: "🔄",
    JobState.DONE: "✅",
    JobState.FAILED: "❌",
    JobState.CANCELLED: "⏹️",
}

COST_BADGE = {
    FREE: "local",
    NETWORK: "network",
    PAID: "💵 costs money",
}

LEVEL_ICON = {"info": "·", "warn": "⚠", "error": "✖"}

# How often the live view redraws while a job runs. A step that names the
# company it is fetching changes on that timescale; faster only costs reruns.
REFRESH_SECONDS = 1.0


# ----------------------------------------------------------------------
# Pure helpers (unit tested)
# ----------------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def step_caption(record: StepRecord) -> str:
    """The one line under a step's name: what it did, or why it did not."""
    if record.state == StepState.PENDING:
        return "waiting"
    if record.state == StepState.RUNNING:
        base = record.message or "working"
        if record.total:
            return f"{base} ({record.done}/{record.total})"
        return base
    if record.state == StepState.SKIPPED:
        return record.message or "skipped"
    if record.state == StepState.FAILED:
        return record.error or "failed"
    if record.state == StepState.CANCELLED:
        return "cancelled mid-step"
    return format_duration(record.duration_seconds)


def job_headline(job: JobRecord) -> str:
    done = sum(1 for s in job.steps if s.is_terminal)
    icon = JOB_ICON.get(job.state, "·")
    return (
        f"{icon} {job.title} — {job.state.value}, {done}/{len(job.steps)} steps, "
        f"{format_duration(job.duration_seconds)}"
    )


def events_frame(job: JobRecord, limit: int = 400) -> pd.DataFrame:
    """The job log as a table, newest last."""
    rows = [
        {
            "time": e.ts.strftime("%H:%M:%S"),
            "": LEVEL_ICON.get(e.level, "·"),
            "step": e.step or "—",
            "message": e.message,
        }
        for e in job.events[-limit:]
    ]
    return pd.DataFrame(rows, columns=["time", "", "step", "message"])


def instrument_options(df: pd.DataFrame) -> list[str]:
    """Picker labels — "RELIANCE — Reliance Industries Ltd"."""
    if df.empty:
        return []
    return [
        f"{r.nse_symbol} — {r.name}" if r.name else str(r.nse_symbol)
        for r in df.itertuples(index=False)
    ]


def symbol_from_option(option: str) -> str:
    return option.split("—")[0].strip()


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _step_picker(scope: str) -> list[str]:
    """Checkbox per available step. Returns the selected keys."""
    specs = step_registry.available_steps(scope)
    selected: list[str] = []

    for spec in specs:
        # `resolve` is not optional — everything downstream needs the ISIN it
        # produces — so it is shown as fixed rather than offered as a choice.
        fixed = spec.key == "resolve"
        cols = st.columns([0.05, 0.95], vertical_alignment="top")
        with cols[0]:
            if fixed:
                st.markdown("🔒")
                checked = True
            else:
                checked = st.checkbox(
                    spec.label,
                    value=spec.default_on,
                    key=f"step_{scope}_{spec.key}",
                    label_visibility="collapsed",
                )
        with cols[1]:
            badge = COST_BADGE.get(spec.cost, spec.cost)
            st.markdown(f"**{spec.label}**  ·  :gray[{badge}]")
            st.caption(spec.description)
        if checked:
            selected.append(spec.key)

    return selected


def _options_form(scope: str) -> RunOptions:
    opts = RunOptions()
    with st.expander("Options", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            opts.index_name = st.text_input(
                "Index", value=settings.default_index, key=f"index_{scope}",
                help="Scoring is sector-relative within this universe.",
            )
            opts.price_years = st.number_input(
                "Years of prices", 1, 20, value=6, key=f"px_years_{scope}"
            )
            opts.filing_years = st.number_input(
                "Years of annual reports", 1, 10,
                value=settings.filing_years, key=f"fy_{scope}",
            )
            opts.as_of = st.date_input(
                "Score as of", value=dt.date.today(), key=f"asof_{scope}",
                help="The decision date. Only data filed on or before it is used.",
            )
        with c2:
            opts.redo_extraction = st.checkbox(
                "Redo annual reads already done", value=False, key=f"redo_{scope}",
                help=(
                    "Re-reads XBRL filings this pipeline previously refused — "
                    "which is the only way a parser fix reaches them."
                ),
            )
            opts.min_coverage = st.slider(
                "Minimum coverage to score", 0.0, 1.0, value=0.5, step=0.05,
                key=f"cov_{scope}",
                help=(
                    "Fraction of model weight that must be backed by data "
                    "before a company is scored at all. Lower it deliberately "
                    "to run on partial data."
                ),
            )
    return opts


def _cost_warning(selected: list[str], opts: RunOptions) -> None:
    paid = [
        step_registry.STEPS_BY_KEY[k]
        for k in selected
        if step_registry.STEPS_BY_KEY[k].cost == PAID
    ]
    if not paid:
        return
    lines = []
    if "narrative" in selected:
        lines.append("**Narrative** is one short call — a few cents.")
    st.warning("This run spends money.\n\n" + "\n\n".join(f"- {line}" for line in lines))


def _render_step(record: StepRecord, expanded: bool) -> None:
    icon = STATE_ICON.get(record.state, "·")
    title = f"{icon}  {record.label}  ·  {step_caption(record)}"

    with st.expander(title, expanded=expanded):
        if record.state == StepState.RUNNING and record.total:
            st.progress(
                min(record.done / record.total, 1.0),
                text=record.message or f"{record.done}/{record.total}",
            )
        elif record.state == StepState.RUNNING:
            st.caption(record.message or "working…")

        if record.summary:
            values = list(record.summary.items())
            for chunk_start in range(0, len(values), 4):
                chunk = values[chunk_start : chunk_start + 4]
                for col, (label, value) in zip(st.columns(len(chunk)), chunk, strict=True):
                    col.metric(label, value if value is not None else "—")

        if record.rows:
            st.dataframe(
                pd.DataFrame(record.rows), width="stretch", hide_index=True
            )

        if record.error:
            st.error(record.error)
        elif record.state == StepState.SKIPPED and record.message:
            st.info(record.message)

        if record.state == StepState.DONE and not record.summary and not record.rows:
            st.caption("Completed with nothing to report.")


def _render_log(job: JobRecord) -> None:
    frame = events_frame(job)
    if frame.empty:
        return
    with st.expander(f"Log ({len(job.events)} lines)", expanded=False):
        st.dataframe(frame, width="stretch", hide_index=True, height=320)

    problems = [e for e in job.events if e.level in ("warn", "error")]
    if problems:
        with st.expander(f"⚠ Warnings and errors ({len(problems)})", expanded=True):
            for e in problems[-25:]:
                text = f"**{e.step or 'job'}** — {e.message}"
                if e.level == "error":
                    st.error(text)
                else:
                    st.warning(text)


@st.fragment(run_every=REFRESH_SECONDS)
def _live_job_view() -> None:
    """Redraws itself while a job runs, then hands control back to the page.

    A fragment reruns in isolation, so the picker above keeps its state and the
    rest of the page is not rebuilt once a second. When the job reaches a
    terminal state the fragment triggers one full rerun so the controls
    re-enable, and stops refreshing.
    """
    job = runner.current
    if job is None:
        return
    snapshot = job.snapshot()
    _render_job(snapshot)

    if not runner.is_active():
        st.rerun(scope="app")


def _render_job(job: JobRecord) -> None:
    st.markdown(f"#### {job_headline(job)}")

    if job.state == JobState.RUNNING:
        finished = sum(1 for s in job.steps if s.is_terminal)
        st.progress(finished / max(len(job.steps), 1))
        if job.cancel_requested:
            st.info(
                "Cancelling — the step in flight finishes its current request "
                "first. Nothing already written is rolled back."
            )
    elif job.state == JobState.FAILED:
        st.error(
            f"{job.error}\n\nSteps before the failure completed and their data "
            f"is saved. Later steps did not run."
        )
    elif job.state == JobState.CANCELLED:
        st.info("Cancelled. Everything completed before that point is saved.")
    elif job.state == JobState.DONE:
        st.success(f"Finished in {format_duration(job.duration_seconds)}.")

    running_key = next(
        (s.key for s in job.steps if s.state == StepState.RUNNING), None
    )
    for record in job.steps:
        # Open the step in flight, and anything that went wrong. A finished run
        # otherwise opens twelve panels at once.
        expanded = (
            record.key == running_key
            or record.state == StepState.FAILED
            or (running_key is None and record.state == StepState.DONE
                and bool(record.summary))
        )
        _render_step(record, expanded)

    _render_log(job)


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------


def render_live_job() -> bool:
    """Draw the current job wherever it is called from. False if there is none.

    The Instrument page can start a gap-filling run, and sending the operator to
    a different page to watch it would mean losing the readiness report that
    prompted it. Same renderer either way, so the two views cannot disagree
    about what a step did.
    """
    if runner.current is None:
        return False
    if runner.is_active():
        _live_job_view()
    else:
        _render_job(runner.current.snapshot())
    return True


def show_run(db: Database) -> None:
    st.subheader("Run the pipeline")
    st.caption(
        "Every step below is the same code its CLI command runs. Steps are in "
        "dependency order and a failure stops the run — extraction after a "
        "failed download would report a confidence score for stale filings."
    )

    active = runner.is_active()
    if active:
        st.info(
            "A job is running. DuckDB allows one writer, so the next job can "
            "start when this one finishes.",
            icon="🔄",
        )

    scope = st.radio(
        "What to update",
        ["One company", "Whole universe"],
        horizontal=True,
        key="run_scope",
    )
    scope_key = "company" if scope == "One company" else "universe"

    symbol = None
    if scope_key == "company":
        instruments = db.query(
            "SELECT nse_symbol, name FROM instruments "
            "WHERE nse_symbol IS NOT NULL ORDER BY nse_symbol"
        )
        options = instrument_options(instruments)
        if not options:
            st.warning(
                "No instruments seeded. Run a **Whole universe** job with "
                "*Refresh index membership* ticked, or `stockanalysis "
                "seed-universe`, before updating a single company."
            )
        else:
            choice = st.selectbox("Company", options, key="run_symbol")
            symbol = symbol_from_option(choice)
    else:
        st.warning(
            "A universe run hits every seeded company in sequence with a "
            f"{settings.request_delay_seconds}s pause between requests — hours, "
            "not minutes. Aggressive parallelism is what gets the IP blocked.",
            icon="⏳",
        )

    st.markdown("##### Steps")
    selected = _step_picker(scope_key)
    opts = _options_form(scope_key)
    _cost_warning(selected, opts)

    c1, c2 = st.columns([0.25, 0.75])
    with c1:
        start = st.button(
            "▶ Start run",
            type="primary",
            disabled=active or (scope_key == "company" and not symbol),
            width="stretch",
        )
    with c2:
        if active and st.button("⏹ Cancel", width="stretch"):
            runner.cancel()
            st.rerun()

    if start:
        try:
            plan = (
                step_registry.company_plan(symbol, selected, opts)
                if scope_key == "company"
                else step_registry.universe_plan(selected, opts)
            )
        except ValueError as e:
            st.error(str(e))
            return

        if not plan.steps:
            st.error("Nothing selected — tick at least one step.")
            return

        try:
            runner.start(plan)
        except JobAlreadyRunning as e:
            st.error(str(e))
            return
        st.rerun()

    st.divider()

    if runner.current is None:
        st.caption("No run yet this session.")
        return

    if runner.is_active():
        _live_job_view()
    else:
        # Finished: render once, statically. A fragment refreshing every second
        # against a job that will never change again is pure overhead.
        _render_job(runner.current.snapshot())
