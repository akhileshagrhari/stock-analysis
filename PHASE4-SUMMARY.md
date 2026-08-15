# Phase 4 — Serving

**Scope:** DESIGN §9 phase 4 — FastAPI endpoints, Streamlit dashboard, LLM
narrative generation, daily scheduled run, alerting.

**Status:** three of five components built, plus a run console DESIGN did not ask for.

| # | Component | State |
|---|---|---|
| 1 | LLM narrative generation | ✅ built |
| 2 | FastAPI endpoints | ✅ built |
| 3 | Streamlit dashboard | ✅ built |
| 4 | Daily scheduled run | ⏳ deferred to phase 4b |
| 5 | Alerting on signal changes | ⏳ deferred to phase 4b |
| — | Run console (operator UI) | ✅ built — see §4 |

Phase 4 is **not complete** until 4 and 5 land. The exit criterion in DESIGN
names all five.

---

## Architecture

Both serving surfaces read through one query layer. Neither reimplements the
other's SQL, and the dashboard does **not** call the API over HTTP — it is a
single-user local tool, and requiring a second process before the UI works buys
nothing.

```
                signals / factor_scores / news  (DuckDB)
                                 |
                       serve/queries.py            <- all SQL lives here
                       JSON-safe row types
                          /            \
              serve/api.py              serve/dashboard.py
              (FastAPI, read-only)      (Streamlit, read-only)
                          \                    |
                           \          serve/explain.py    <- why a signal is
                            \         (arithmetic, free)     what it is
                     serve/narrative.py            <- writes signals.narrative
                     (Claude, during `persist`)
```

`queries.py` exists because the same questions were being asked twice in two
files, which is how an API and a dashboard end up disagreeing about what the
model said.

---

## 1. Narrative generation

`src/stockanalysis/serve/narrative.py`

Claude explains a rating it cannot revise. The score is arithmetic; only the
prose is generated, and nothing here feeds back into a number.

Run it as part of scoring:

```python
from stockanalysis.db.database import Database
from stockanalysis.factors.composite import persist, score_as_of

db = Database("data/stockanalysis.duckdb")
result = score_as_of(db, "NIFTY100", dt.date(2026, 7, 31))
persist(db, result, generate_narratives=True)   # off by default
```

**Model configuration** (all in `config.py`, `SA_`-prefixed env vars):

| Setting | Default | Why |
|---|---|---|
| `narrative_model` | `claude-opus-5` | |
| `narrative_effort` | `low` | A narrative restates a computed score in two sentences. Effort is the cost lever; a fixed thinking budget is rejected by this model family. |
| `narrative_max_tokens` | `1000` | Caps thinking *and* response together. Thinking is on by default on Opus 5, so a budget sized to the ~120-token answer alone would truncate. |
| `narrative_max_workers` | `4` | |
| `narrative_news_window_days` | `30` | Matches the sentiment factor's window. |

**Cost control.** The stable half of the prompt — model description, weights,
flag rules — is a cached system prompt; only the per-company block varies. The
first call is issued alone so it can write the cache, then the rest fan out and
read it, because a cache entry is only readable once the response that wrote it
has started streaming. Firing the whole universe at once would mean every call
misses and pays full price.

**Failure policy.** A timeout costs one company its narrative. A bad API key
aborts the pass with `NarrativeUnavailable` rather than failing identically a
hundred times — the previous version caught everything and returned `None`,
which made a missing key indistinguishable from a quiet model.

---

## 2. API

`src/stockanalysis/serve/api.py` — `stockanalysis serve-api`

Read-only by construction (`read_only=True`), so no route can write and several
processes can serve the same file at once. Binds to `127.0.0.1` by default: this
is an unauthenticated read surface over the whole research database, and
exposing it should be deliberate.

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | Status, latest scored date, row counts |
| GET | `/model` | Live weights, thresholds, scored dates |
| GET | `/sectors` | Distinct sectors |
| GET | `/instruments` | All instruments, optionally by `sector` |
| GET | `/instruments/{isin}` | One instrument |
| GET | `/instruments/by-symbol/{symbol}` | Resolve an NSE symbol |
| GET | `/instruments/{isin}/latest` | Latest signal + factor breakdown |
| GET | `/instruments/{isin}/history` | Signal history, oldest first |
| GET | `/instruments/{isin}/news` | Recent news with sentiment labels |
| GET | `/signals` | Signals for a date; filter by `signal`, `sector`, `as_of` |
| GET | `/signals/red-flags` | Names with a tripped flag |

Interactive docs at `/docs`.

Three properties worth knowing:

- **`composite_score` and `signal` may be null.** A company whose coverage fell
  below the model's floor is unscored, and unscored is not HOLD. The API says so
  rather than inventing a number.
- **Responses are strict-JSON safe.** DuckDB returns NaN for a NULL DOUBLE, and
  NaN is not valid JSON. `queries.py` converts at the boundary, so
  `Optional[float]` can be trusted.
- **The database arrives by dependency injection.** That is what makes the
  endpoints testable against a temporary database, and it is where a missing,
  locked, or out-of-date database becomes a 503 with an explanation instead of a
  traceback.

---

## 3. Dashboard

`src/stockanalysis/serve/dashboard.py` — `stockanalysis dashboard`

Pages: **Overview**, **Signals**, **Instrument**, **Red flags**, **About**.

### Why a signal is what it is

`src/stockanalysis/serve/explain.py`

Every signal comes with its reasoning, on the Instrument page under **Why this
signal**:

- a verdict naming the threshold crossed, or the flag that overrode it
  ("SELL — forced by the red-flag overlay (promoter_selling), overriding a
  factor score of 81/100");
- the case in sentences — which families pushed the score up or down, the
  strongest single factor for and against, the news mix;
- **where the score came from**: each family's percentile and its signed
  contribution to the composite, which sum to it exactly;
- **arguing for it / arguing against it**: individual factors with the
  company's own values and their distance from the sector mean.

The Signals browser carries a **Main driver** column (`Growth ↑`, `Momentum ↓`)
so the same question is answered for every name at once.

Three things make this trustworthy rather than decorative:

- **It is reconstructed from `factor_scores`, not re-scored.** Replaying the
  model's aggregation over the stored z-scores reproduces the composite to
  floating-point equality — verified against a fresh scoring run in the tests.
- **It is free and always available.** Unlike the LLM narrative, it costs
  nothing and needs no API key, so an explanation is never missing.
- **It says what it could not see.** An unmeasured family reads as "not
  measured", never as average; unevaluable red flags are named; and news outside
  the 30-day scoring window is marked as not having counted.

Stored z-scores are already sign-adjusted, so "+1.0 SD" is good whether the
underlying metric is return on equity or debt/equity — the module relies on that
and says so. When a stored signal came from a different scoring config, the page
says the contributions are approximate rather than quietly re-deriving them
under today's weights.

- Overview counts BUY/HOLD/SELL **and unscored** — how much of the universe the
  model could not see belongs next to the other three numbers.
- Instrument shows score, factor breakdown, history, narrative, flags with their
  rule text, and recent news.
- Red flags names the flags no ingest path currently supplies, so their absence
  is not read as a clean bill of health.
- About reads weights, thresholds and flag rules from the running model rather
  than transcribing them, so the page cannot drift out of date.

The connection is opened per script run, not cached. Streamlit shares
`st.cache_resource` objects across sessions and threads, and a DuckDB connection
is not safe to use that way.

---

## 4. Run console

`src/stockanalysis/run/` + `src/stockanalysis/serve/ops.py` — the dashboard's
**Run** page, and `stockanalysis update` for the same thing headless.

Until this, every pipeline stage was a separate CLI command and the dashboard
could only read what those commands left behind. The console runs them as one
declared job and reports each step as it happens: which endpoint is in flight,
how many rows landed, and the confidence score every extracted annual report
came back with.

```
   run/steps.py        the registry: order, cost, what each step reports
        |              (wraps the same functions the CLI commands call)
   run/runner.py       execute_plan  ->  synchronous, owns nothing
        |              JobRunner     ->  one background job, owns its connection
   run/events.py       JobRecord / StepRecord / Reporter
        |              worker mutates; readers take a snapshot
   serve/ops.py        the page — picker, cost warning, live step view
```

### One writer, and what that forces

DuckDB permits a single writer. Three consequences, all of them structural
rather than stylistic:

- **Jobs do not queue, they refuse.** A second `start()` raises rather than
  waiting, because a queued job would fail on the lock later — after the
  operator had walked away, and possibly after extraction had already spent
  money.
- **The worker owns the write connection** for the life of the job. DuckDB
  connections are not safe across threads, so the thread doing the writing holds
  the handle.
- **Readers join rather than wait.** A read-only open *in the same process*
  while that writable connection exists is refused by DuckDB for configuration
  reasons — a distinct failure from another process holding the file, and one
  that would blank the dashboard for the entire duration of every job.
  `Database.connect_for_read` asks for read-only and joins the writable instance
  if that is refused, which is why the other pages keep working while a job
  runs. It is now `SameProcessConfigError`, not an unexplained traceback.

### What the reporting is for

A console that says "done" for work that did not happen is worse than no console.
So:

- **A step that could not run reports SKIPPED with the reason** — no API key,
  nothing pending, NSE has no reports for this symbol — and the job continues.
  Never DONE.
- **A failure stops the job**, and every later step is marked *not reached*
  rather than left pending. Steps are in dependency order: extraction after a
  failed download would report a confidence score for stale filings.
- **Extraction reports per filing, not per run.** Confidence, the band it falls
  in (auto-accept / flagged / human review), which validator failed and by how
  much, cost, latency. An average across three reports would hide the one that
  failed the accounting identity.
- **Scoring says what it could not see.** An unmeasured family reads as "not
  counted", never as 50. A company below the coverage floor reads as *unscored*,
  with the reminder that unscored is not HOLD.
- **Cost is declared before the run.** Only `extract` and `narrative` spend
  money, both are off unless ticked, and the page prices the run from DESIGN
  §5.4's per-report figure before the button is live.

Scoring runs over the whole index even for a single-company job, because every
factor is a sector-relative z-score and a company cannot be ranked against
itself. The page says so rather than implying the number came from that
company's data alone.

**Cancellation is cooperative.** It takes effect at the next checkpoint a step
offers — between filings, between companies — because the ingest calls are
ordinary blocking functions with no abort channel. A filing already sent to the
model is paid for either way, so it finishes and persists rather than being
abandoned.

### Progress hooks

`ingest_prices`, `ingest_quarterly`, `ingest_shareholding` and
`fetch_annual_reports` gained an optional `progress(index, total, symbol)`
called *before* each request, and `fetch_annual_reports` an `on_document` hook
per registered PDF. Firing before rather than after is deliberate: on a crawl
with a 1s pause between requests, almost all the elapsed time is spent inside
the call, and the useful thing to display is the company in flight rather than
the one that already finished.

### Relation to phase 4b

The scheduled run is the same `Plan` on a timer. That is the reason the plan is
a value and `execute_plan` takes a database it does not own — APScheduler needs
no part of the UI, and the UI needs no part of the scheduler.

---

## Testing

`tests/test_phase4_serving.py` — 88 tests.
`tests/test_pipeline_runs.py` — 55 tests for the runner, the steps and the page,
including one that clicks Start through `AppTest` and asserts the job actually
ran and rendered. The runner tests were checked by mutation: disabling
*not reached* marking, reporting a skip as success, removing the read-connection
fallback, reporting an unmeasured family as 50, and allowing concurrent jobs each
make them fail.

The previous suite asked `seeded_db` for the `NIFTY100` universe while the
fixture seeds `TESTIDX`. That returns an empty list, so nothing was scored,
`n_signals` was always zero, and every assertion sat behind `if n_signals > 0:`.
It passed by testing nothing.

What is covered now:

- **Read layer** — NaN/NA coercion, ordering, filters, batched sentiment counts.
- **API** — every endpoint against a seeded database, 404/422 paths, strict-JSON
  parsing of the payload, a 503 when the database is absent, and a route-ordering
  guard that asks Starlette which route wins rather than comparing path shapes.
- **Narrative** — prompt construction, the cached prefix staying free of
  per-company data, refusal handling, per-company vs pass-fatal errors.
- **Dashboard** — every page executed via `AppTest`. Importing the module proves
  almost nothing: page code only runs when a session renders it.

Assertions are unconditional by design. Each was checked by mutation — breaking
the code it covers makes it fail.

---

## Known limitations

1. **No scheduled run and no alerting** — phase 4b.
2. **Narratives are opt-in**, generated during `persist`, not backfilled.
3. **The score is relative**, and every surface says so. 80 means "near the top
   of this universe today", not "cheap".
4. **The API is unauthenticated.** Loopback by default; do not bind it to a
   public interface as-is.
5. **Job history does not survive a restart.** The console keeps the current
   run in memory; a finished job is gone when Streamlit restarts. What the job
   *wrote* is in the database — this is the log, not the data. Persisting a
   `job_runs` table is the natural companion to 4b's alert history.
6. **The run console has no authentication either**, and unlike the API it can
   spend money. Do not expose the dashboard beyond loopback.
7. **A universe run is serial and slow by design** — one company at a time with
   a configured pause. Parallelising it is what gets the IP blocked, which is
   the principal operational risk in DESIGN §10.

---

## Phase 4b

**Daily scheduled run:** ingest prices and news, score, persist, on a timer.

**Alerting:** detect signal transitions and newly tripped flags between
consecutive scored dates, and notify. Needs an alert-history table so a change is
reported once rather than on every run.
