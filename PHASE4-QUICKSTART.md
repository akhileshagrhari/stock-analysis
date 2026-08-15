# Phase 4 — Quick Start

Serving the signals phase 2 computed: an HTTP API, a browser dashboard, and
optional written explanations.

## Install

`fastapi`, `uvicorn` and `streamlit` are declared in `pyproject.toml`:

```bash
uv pip install -e ".[dev]"
```

## Prerequisites

Phase 4 serves what phases 0–2 produced. If `stockanalysis status` shows no
signals:

```bash
stockanalysis init
stockanalysis seed-universe --index NIFTY100
stockanalysis ingest-prices --years 6
stockanalysis score --as-of 2026-07-31 --min-coverage 0.15
```

---

## 1. API

```bash
stockanalysis serve-api                 # http://127.0.0.1:8000
stockanalysis serve-api --port 9000     # or elsewhere
```

Interactive docs at `/docs`. Binds to loopback by default — it is an
unauthenticated read surface over the whole research database, so pass
`--host 0.0.0.0` only deliberately.

```bash
curl localhost:8000/health
curl localhost:8000/signals
curl "localhost:8000/signals?signal=BUY&limit=10"
curl "localhost:8000/signals?sector=Bank&as_of=2026-07-31"
curl localhost:8000/signals/red-flags
curl localhost:8000/instruments/by-symbol/RELIANCE
curl localhost:8000/instruments/INE002A01018/latest
curl localhost:8000/instruments/INE002A01018/history
```

From Python — note the null check, which is not decoration:

```python
import requests

for row in requests.get("http://localhost:8000/signals", timeout=30).json():
    # A company whose coverage fell below the model's floor has no score.
    # Unscored is not HOLD, and formatting None as a float raises.
    score = "unscored" if row["composite_score"] is None else f"{row['composite_score']:.1f}"
    flags = ", ".join(row["red_flags"]) or "-"
    print(f"{row['nse_symbol']:14s} {score:>8s}  {row['signal'] or '-':5s}  {flags}")
```

### Response shape

```json
{
  "isin": "INE002A01018",
  "nse_symbol": "RELIANCE",
  "name": "Reliance Industries Limited",
  "sector": "Refineries",
  "as_of": "2026-07-31",
  "composite_score": 62.5,
  "signal": "HOLD",
  "coverage": 0.75,
  "red_flags": ["weak_cash_conversion"],
  "unknown_flags": ["promoter_pledge", "rating_downgrade"],
  "narrative": "Ranks well on quality...",
  "model_version": "phase2-composite-abc12345"
}
```

- `red_flags` and `unknown_flags` are **lists**, not comma-joined strings.
- `composite_score` and `signal` are **nullable** — see above.
- `unknown_flags` are flags that could not be evaluated for want of data. Their
  absence from `red_flags` is not a clean bill of health.
- `/instruments/{isin}/latest` adds a `factors` array of
  `{factor_name, raw_value, sector_zscore}`.

---

## 2. Dashboard

```bash
stockanalysis dashboard                 # http://localhost:8501
stockanalysis dashboard --port 8600
```

Pages: **Overview** (signal counts including unscored), **Signals** (filter by
date, signal, sector; search; CSV export), **Instrument** (score, reasoning,
factor breakdown, history, flags, news), **Red flags**, **About** (weights and
rules read from the running model).

### Seeing why a signal is what it is

The Instrument page opens with **Why this signal**:

- the verdict and what caused it — the threshold crossed, or the red flag that
  overrode the factors;
- which families pushed the score up or down, with each one's percentile and its
  signed contribution to the composite (they sum to it exactly);
- the strongest individual factors **arguing for** and **arguing against**, with
  the company's own values and their distance from the sector mean;
- the 30-day news mix, with anything outside the scoring window marked as not
  having counted.

The Signals browser adds a **Main driver** column (`Growth ↑`, `Momentum ↓`) so
you can scan the reason for every name at once.

This is derived from the stored factor values, so it is free, always present,
and needs no API key — distinct from the optional LLM narrative below, which is
a written summary of the same numbers.

Two things it will tell you that are easy to miss: a family shown as *not
measured* had no data and was **not** scored as average, and red flags listed as
not evaluable mean the data was missing — their absence is not a clean bill of
health.

The dashboard reads the database directly through the same query layer the API
uses. It does **not** require the API to be running.

---

## 3. Narratives (optional, costs money)

Off by default. Enable during scoring:

```python
import datetime as dt

from stockanalysis.db.database import Database
from stockanalysis.factors.composite import persist, score_as_of

db = Database("data/stockanalysis.duckdb")
result = score_as_of(db, "NIFTY100", dt.date(2026, 7, 31))   # a date, not a string
persist(db, result, generate_narratives=True)
db.close()
```

Credentials come from the `anthropic` SDK's own resolution — `ANTHROPIC_API_KEY`
or an `ant auth login` profile. An invalid key aborts the pass immediately rather
than failing once per company.

Tune via `SA_`-prefixed env vars or `.env`:

```bash
SA_NARRATIVE_MODEL=claude-opus-5
SA_NARRATIVE_EFFORT=low        # the cost lever
SA_NARRATIVE_MAX_WORKERS=4
```

**Cost.** Roughly **$1 per 100 signals** on Opus 5 ($5/M input, $25/M output),
dominated by output rather than input: the shared ~600-token system prompt is
cached and read at about a tenth of input price, so each call pays for ~250
tokens of company data in and a few hundred out. Treat that as an order of
magnitude, not a quote — read `usage` off a real run to measure it. Lower
`SA_NARRATIVE_EFFORT` reduces it; a fixed thinking budget is rejected by this
model family.

---

## Daily run (manual until phase 4b)

The scheduled run is not built yet. Until it is:

```bash
#!/bin/bash
set -euo pipefail
cd /path/to/StockAnalysis

stockanalysis ingest-prices
stockanalysis ingest-news
stockanalysis score-news
stockanalysis score --as-of "$(date +%Y-%m-%d)" --min-coverage 0.15
```

```
0 22 * * * /path/to/daily-update.sh >> /tmp/stockanalysis.log 2>&1
```

Run the API and dashboard in separate terminals; both open the database
read-only, so they coexist. Neither can run during a write command — DuckDB
allows one writer, and an ingest in progress surfaces as a 503 or a dashboard
warning rather than a crash.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| API returns 503 | No database, an ingest holds the write lock, or the file predates the current schema. The message says which; `stockanalysis init` fixes the last. |
| "No signals stored yet" | Scoring has not run. `stockanalysis score --as-of <date>`. |
| Score is `null`, signal is `null` | Coverage below the model's floor. Lower `--min-coverage` deliberately, or ingest the missing fundamentals. |
| Narratives all empty | `generate_narratives=True` was never passed, or the pass aborted — check the log for `NarrativeUnavailable`. |
| 404 on `/instruments/{isin}` | Wrong ISIN. Resolve by symbol: `/instruments/by-symbol/RELIANCE`. |
