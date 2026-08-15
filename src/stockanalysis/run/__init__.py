"""Pipeline orchestration — running the ingest/extract/score steps as one job.

Everything here is a thin coordinator over code that already existed as CLI
commands. It adds three things the CLI could not give a UI: a *declared* step
list that can be shown before anything runs, structured progress events instead
of console prose, and a background worker so a browser can watch a job that
takes minutes.

No step reimplements ingest or scoring logic. If a step and its CLI command
disagree, that is a bug in the step.
"""

from stockanalysis.run.events import Event, JobRecord, JobState, Reporter, StepRecord, StepState
from stockanalysis.run.runner import JobRunner, execute_plan, runner
from stockanalysis.run.steps import STEPS, Plan, StepSpec, company_plan, universe_plan

__all__ = [
    "STEPS",
    "Event",
    "JobRecord",
    "JobRunner",
    "JobState",
    "Plan",
    "Reporter",
    "StepRecord",
    "StepSpec",
    "StepState",
    "company_plan",
    "execute_plan",
    "runner",
    "universe_plan",
]
