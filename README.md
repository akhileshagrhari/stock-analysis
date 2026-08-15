# StockAnalysis

Factor-based equity research for Indian markets (NSE/BSE). See [DESIGN.md](DESIGN.md)
for the full architecture and roadmap.

**Phase 0 is complete**: data spine, price ingest, a walk-forward backtest harness
with realistic Indian transaction costs, and the correctness tests that decide
whether any of it can be believed.

**Phase 1 is built**: annual-report downloader, PyMuPDF section locator, Claude
structured extraction with arithmetic validators, a model bake-off, and a human
review queue. It has not yet been run against live data — see
[Phase 1 status](#phase-1-extraction) below.

**Phase 2 is built and measured**: all five factor families, sector-relative
z-scoring, the red-flag overlay with an explicit tri-state, composite scoring,
and per-factor attribution. Fifteen of its twenty factors read the annual-report
fundamentals phase 1 has not yet backfilled, so the model runs at **23% data
coverage** and says so on every run. It does not beat the index, and the shuffled
control does slightly better than it does — see
[PHASE2-FINDINGS.md](PHASE2-FINDINGS.md).

**Phase 3 is built and measured**: RSS ingest across four outlets, a ticker
resolver built for precision over recall, FinBERT sentiment scoring running
locally on CPU/MPS, and a checkpointed GDELT historical backfill. The scoring
half works end to end. The **history** half does not yet: GDELT's public API
throttles far below its documented rate, so the backfill is an overnight job
rather than a command, and until it completes the sentiment factor is still
live-only and still not backtestable — see
[PHASE3-FINDINGS.md](PHASE3-FINDINGS.md).

**Phase 4 is three-fifths built**: LLM narrative generation (Claude explains a
signal it cannot revise), a read-only FastAPI surface over instruments, signals,
red flags, history and news, and a Streamlit dashboard. Both serving surfaces
read through one shared query layer, so they cannot disagree about what the model
said. Narratives are optional and the only part that costs money; the API and
dashboard are free local services. **The scheduled daily run and alerting are not
built** — DESIGN §9 names all five, so the phase is not complete — see
[PHASE4-SUMMARY.md](PHASE4-SUMMARY.md) and
[PHASE4-QUICKSTART.md](PHASE4-QUICKSTART.md).

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env
```

## Quick start

```bash
stockanalysis init                                            # create DB + dirs
stockanalysis seed-universe --index NIFTY100 \
                            --backfill-from 2018-01-01        # fetch constituents
stockanalysis ingest-prices --years 6                         # ~3 min, be patient
stockanalysis backtest --start 2021-01-01 --top-n 20
stockanalysis status                                          # what's loaded
```

Phase 2 — the factor model:

```bash
stockanalysis score --as-of 2026-07-31 --min-coverage 0.15    # rank the universe
stockanalysis attribution --start 2021-06-30                  # which factors work
stockanalysis backtest --factor composite --min-coverage 0.15
```

Phase 3 — news and sentiment, all free and keyless except Marketaux:

```bash
stockanalysis build-aliases                   # headline text -> ISIN, run first
stockanalysis ingest-news                     # RSS: today's front pages
stockanalysis score-news                      # FinBERT, local, CPU/MPS
stockanalysis news-status                     # attribution + monthly coverage
stockanalysis backfill-news --start 2023-01-01 --max-requests 500   # GDELT, slow
stockanalysis reresolve-news                  # re-attribute stored text
```

Phase 4 — serving (API + dashboard):

```bash
stockanalysis serve-api                       # FastAPI on 127.0.0.1:8000, docs at /docs
stockanalysis dashboard                       # Streamlit on :8501
```

With optional narrative generation (credentials resolved by the `anthropic` SDK):

```bash
python3 -c "
import datetime as dt
from stockanalysis.db.database import Database
from stockanalysis.factors.composite import score_as_of, persist
db = Database('data/stockanalysis.duckdb')
result = score_as_of(db, 'NIFTY100', dt.date(2026, 7, 31))
persist(db, result, generate_narratives=True)  # Claude explains each signal
db.close()
"
```

Phase 1b — free, no API key, no model:

```bash
stockanalysis ingest-quarterly --limit 10     # revenue / PAT / EPS per quarter
stockanalysis ingest-results-index --years 3  # real filing dates, exchange-wide
stockanalysis ingest-shareholding --limit 10  # promoter holding
```

Phase 1 extraction — needs `ANTHROPIC_API_KEY` and spends money:

```bash
stockanalysis fetch-filings --years 3 --limit 10   # annual report PDFs from NSE
stockanalysis bakeoff --n 10                       # Opus 5 vs Sonnet 5, ~$5
stockanalysis extract --limit 5                    # synchronous, one at a time
stockanalysis extract-batch --limit 300            # backfill at 50% off
stockanalysis review                               # what needs a human
```

Or without an API key, through the Claude Code CLI on your subscription
(see [no API key](#extraction-without-an-api-key)):

```bash
stockanalysis extract --limit 5 --model cli:              # cli:claude-opus-5
stockanalysis bakeoff --n 10 --models cli:claude-opus-5,cli:claude-sonnet-5
```

Or free, on a local model via LM Studio (see [the cost floor](#the-cost-floor)):

```bash
lms server start
stockanalysis local-models                          # what's loaded
stockanalysis extract --limit 5 --model local:<id>
stockanalysis bakeoff --n 10 --models local:<id>    # measure how bad it is
```

## First real result

Nifty 100, Jan 2022 – Aug 2026, top 20 by 12-1 momentum, monthly rebalance,
transaction costs on:

```
                          Momentum   EW hold
  CAGR                      18.50%    20.29%
  Volatility                19.33%    15.69%
  Sharpe                      0.62      0.88
  Max drawdown             -25.64%   -20.33%
  [-] Momentum LAGGED equal-weight hold by 1.79%/yr
```

**Momentum lost to simply holding the universe** — less return, more volatility,
deeper drawdown. Read in isolation, "18.5% CAGR" looks like a win; that is
exactly why no headline number in this repo is reported without its benchmark.

This is a Phase 0 result, so treat it as a working harness rather than a verdict
on momentum: the window is short, survivorship-unsafe (below), and covers one
strong bull market in Indian equities.

## Why the backtest is built before the recommender

A factor model is easy to write and almost impossible to evaluate by eye. Phase 0
therefore ships a harness driven by one deliberately boring factor — 12-1
momentum — chosen because its plausible magnitude is *known*. If the harness
reports a Sharpe of 4 on it, the harness is broken.

That paid for itself immediately. The first run reported a CAGR of −36%, which
turned out to be a sign error in portfolio selection: a descending sort followed
by `.tail()` picked the **worst**-ranked names. It raises no exception and looks
exactly like an honest negative result. `test_engine_selects_the_intended_end_of_the_ranking`
now guards it.

## The correctness tests

These are the point of phase 0. Run them before believing any number this
system produces.

| Test | What it defends against |
|---|---|
| `test_factor_value_is_invariant_to_future_data` | Compute a factor, inject future prices, recompute — the value must not move. Catches leaks without assuming *how* they occur. |
| `test_fundamentals_hidden_until_filing_date` | FY2023 figures were not knowable in May 2023. Filters on `filing_date`, never `period_end_date`. |
| `test_delisted_company_losses_are_counted` | A company that stops trading is marked to its last price, not silently dropped — otherwise the backtest deletes exactly the failures it should record. |
| `test_no_alpha_from_signalless_data` | Pure random walks, identical drift and vol. Any alpha here is a leak. |
| `test_measured_alpha_tracks_embedded_signal` | Sharpe must rise monotonically with embedded signal. Positive evidence the number reported is the number measured. |
| `test_shuffled_returns_destroy_alpha` | Sever the selection-to-outcome link; the edge must vanish. |
| `test_membership_coverage_flag_gates_the_warning` | A survivorship-unsafe universe must announce itself. |
| `test_backtest_cannot_see_a_filing_before_it_was_published` | Phase 1's version of the same trap: FY2024 figures describe a year ending 31 March but became public in September. |
| `test_extraction_cannot_override_the_knowledge_date` | The model reads several date-shaped things off a report. None of them can reach `filing_date`. |
| `test_eps_is_never_scaled_by_the_reporting_unit` | Scaling EPS by "(Rs. in lakhs)" turns 84.20 into 0.0000084 and ruins every valuation factor. |
| `test_nse_cross_check_catches_a_wrong_column` | An internally perfect extraction that read the prior-year column throughout. Only external evidence catches it. |
| `test_a_factor_with_no_data_does_not_score_as_average` | Phase 2's version of the same trap. A factor with no data anywhere was being scored 0.0 — "average" — for every company, which reported 92% data coverage on an empty fundamentals table. |
| `test_insufficient_coverage_withdraws_the_score` | A company scored on 15% of the model gets no score, not a weak one. Otherwise the composite silently degenerates into whichever factor happened to have data. |
| `test_an_unavailable_flag_is_unknown_not_clear` | Promoter pledge has no data source. A boolean overlay would report it as clear — a clean bill of health for exactly the companies it exists to catch. |
| `test_injecting_future_fundamentals_does_not_move_todays_score` | The phase-0 leak test applied to all fifteen fundamental factors at once, which is what the shared panel loader is for. |
| `test_a_quarter_filed_twice_in_different_units_yields_one_row` | NSE files the same quarter in both lakhs and crore with nothing declaring which. Picking wrong is a silent 100x error in revenue and PAT. |

```bash
pytest -q
```

## Two limitations to read before trusting output

**The universe is survivorship-unsafe.** NSE publishes *current* constituents.
Every company that collapsed out of the index is already missing, so absolute
backtest returns are biased upward. `--backfill-from` makes backtests runnable by
assuming today's constituents were always constituents — an assumption, not data.
The engine prints a `SURVIVORSHIP UNSAFE` warning on every affected run and
`stockanalysis status` flags it. Relative factor comparisons remain informative;
absolute performance does not.

**This is still open after phase 2.** Fixing it needs historical index
membership — which company was in the Nifty 100 on a given past date — and NSE
publishes only current constituents. The candidates are index-review press
releases (semi-annual, scrapeable, tedious) or a paid data vendor. Neither is a
code change, which is why phase 2 did not close it. The factor attribution
report is the part of phase 2 least affected: rank IC is computed across the
cross-section on each date, so it degrades far more gracefully under a biased
universe than an absolute CAGR does.

**Commands are serial.** DuckDB takes a process-exclusive lock on the database
file, so a second command cannot run during an ingest — not even a read-only one.
You get a clear "Database busy" message rather than a raw `IOException`.

**yfinance is unofficial and will break.** It sits behind the `PriceProvider`
interface precisely so a broker API (Angel One SmartAPI, Upstox) can replace it
without touching anything downstream. Note the ingest layer logs a loud warning
if it ever falls back to unadjusted closes — an unadjusted series shows a 1:2
split as a 50% crash, which momentum reads as real signal.

## Layout

```
src/stockanalysis/
  config.py            settings + Indian transaction-cost model
  db/
    schema.sql         point-in-time contract documented at the top
    database.py        as_of_* — the ONLY sanctioned read path for decisions
  universe/loader.py   NSE constituent fetch, membership intervals
  ingest/
    prices.py          PriceProvider interface + yfinance implementation
    filings.py         FilingProvider interface + NSE annual-report downloader
    nse_fundamentals.py  quarterly results + real filing dates from the index
    shareholding.py    promoter holding (no pledge — see below)
    xbrl.py            structured results facts, no model in the loop
    rss.py             four outlets, hand-rolled feed parser, no history
    marketaux.py       entity-tagged news; a second opinion on attribution
    gdelt.py           historical backfill, checkpointed per company-month
  news/
    aliases.py         what is allowed to be an alias, and what never is
    resolve.py         longest-match scan, headline over body
    store.py           dedupe, IST knowledge dates, one row per (article, company)
    finbert.py         local sentiment; owns the signed-score convention
    scoring.py         score once per unique text, persist per row
  extract/
    factory.py         model string -> backend (API, Claude Code CLI, or local)
    claude_cli.py      `claude -p` path: subscription-billed, no API key
    local.py           LM Studio path: flattened text, the cost floor
    locator.py         PyMuPDF: 300pp -> ~60pp of financial statements
    schema.py          the extraction contract + unit normalisation
    prompts.py         the stable, cached system prompt
    claude.py          messages.parse() + Batch API, cost accounting
    validate.py        arithmetic identities -> confidence score
    pipeline.py        orchestration; enforces the knowledge-date rule
    bakeoff.py         model comparison
    review.py          human review queue
  factors/
    base.py            Factor ABC, MAD winsorization, sector z-scores
    panel.py           one point-in-time snapshot per date, shared by all factors
    value.py           E/P, B/P, EBITDA/EV, FCF yield, PEG — all yield-side up
    quality.py         ROE, ROCE, D/E, interest cover, CFO/PAT, accruals
    growth.py          revenue and PAT CAGR, quarterly YoY, margin trend
    momentum.py        12-1, price/200-DMA, relative strength
    sentiment.py       recency-weighted news score over resolved, scored news
    redflags.py        §6.2 overlay — TRIPPED / CLEAR / UNKNOWN, never a boolean
    composite.py       family weights, coverage rule, 0-100 score, signal
  backtest/            costs, walk-forward engine, metrics, factor attribution
  cli.py
```

## Phase 1: extraction

Turning 300-page PDFs into numbers — where DESIGN argues ML actually earns its
place. The pipeline is split so each half fails loudly:

```
PDF (200-400pp)
  -> locator    PyMuPDF finds the statements       deterministic, ~4x cost cut
  -> claude     reads them into a Pydantic schema  judgement
  -> validate   checks arithmetic identities       deterministic
  -> confidence 1.0 persist | 0.6 persist+flag | 0.0 review queue only
```

Neither half is sufficient alone. A table parser cannot tell you that *this*
balance sheet is the consolidated one and that the second column is the prior
year. A language model cannot be trusted to add up. Putting a deterministic
checker downstream of a probabilistic reader is the whole design.

**Three things worth knowing before running it.**

*Knowledge dates are mostly assumed, not measured.* `fundamentals_annual.filing_date`
decides what a backtest is allowed to see. NSE's annual-report listing does not
reliably carry a broadcast timestamp, so we fall back to the statutory AGM
deadline — period end plus six months. That is deliberately **late**: a knowledge
date that is too late costs some signal, one that is too early manufactures it.
Which of the two was used is recorded in `filings.broadcast_date_source` and
surfaced by `stockanalysis status`, so the difference between data and assumption
stays visible instead of becoming folklore.

*The NSE cross-check is the only score that means much.* Arithmetic identities
catch internally inconsistent extractions. They are blind to a model that
confidently read the standalone column throughout — every identity still holds.
Only comparing against the sum of four quarterly filings catches that, which is
why `ingest-quarterly` should run before the bake-off.

*The model choice is unresolved.* DESIGN says settle it with a bake-off rather
than by argument, and the bake-off is built but has not been run. `stockanalysis
bakeoff --n 10` scores Opus 5 against Sonnet 5 on validator pass rate, NSE
agreement, and cost, and lists the filings where the two models disagree — those
are the rows worth reading by hand.

**Not yet run against live data.** Everything here is tested against synthetic
PDFs and canned NSE payloads; no real annual report has been through it, and no
API call has been made. The exit criterion (>=95% of extractions passing
validation and agreeing with NSE quarterly data) is unmeasured.

## What works without an API key

Claude Pro covers claude.ai and Claude Code; it does not include Developer
Platform credits, which is what the `anthropic` SDK spends. So the extraction
path needs a separate top-up at console.anthropic.com — roughly $5 for the
10-report bake-off and $45–115 for a Nifty 100 × 3yr backfill with batching.

A useful amount of the system does not need it:

| | Source | Reaches |
|---|---|---|
| Daily OHLCV | yfinance | Momentum, all price factors |
| Revenue / PAT / EPS, quarterly | `results_comparison` | Growth, part of Value |
| Real filing dates, exchange-wide | `financial_results` | Point-in-time correctness |
| Structured P&L facts | XBRL, where attached | Same, higher fidelity |
| Promoter holding, quarterly | `shareholding` | One red flag |
| **OCF, balance sheet, auditor opinion, contingent liabilities** | **annual report PDFs** | **Quality, the earnings-quality checks** |

That last row is the one that matters most and the one that costs money. CFO/PAT
and the accruals ratio are what DESIGN calls the most reliable published warning
signs in Indian mid-caps, and they exist only in the cash flow statement.

**Two gaps to know about.** `NSE.shareholding()` returns the holding split but
carries **no pledged-shares figure**, so DESIGN's "promoter pledge > 25%" red
flag is not reachable from it — `promoter_pledged_pct` stays NULL and must be
read as *unknown*, never as zero. And XBRL element naming should be checked
against a real filing before its output is trusted; `xbrl.unmapped_facts()`
reports what the parser saw and ignored, so a missing field is a lookup rather
than a guess.

### Extraction without an API key

`--model cli:<model>` reaches a frontier model through Claude Code's headless
mode (`claude -p`), which your Claude subscription covers. Measured on
Reliance's FY2026 report: **$1.88, 42s, single turn**, with OCF, capex and the
balance sheet extracted correctly.

Three things to know before leaning on it.

*It is the most expensive option per report*, not the cheapest — $1.88 against a
working estimate of ~$0.75 through the API and ~$0.38 batched. Every call is a
cold cache write at 1.25×, there is no Batch API discount, and flattened
statement text tokenises badly (168k characters came to 95k tokens). It is
cheaper only in that it spends a subscription you have already paid for.

*The input is degraded, the same way the local path's is.* The CLI's `Read` tool
rasterises PDFs through poppler, which is absent on a stock macOS box and
ruinous for sixty pages, so pages are flattened to text and column alignment
suffers. Unlike the local path there is **no truncation** — 60 pages come to
~85k tokens, well inside the context window — so the notes survive, and with
them contingent liabilities and the auditor's remarks.

*There is no batching.* One process and ~40s per report, so a Nifty 100 × 3yr
backfill is 300 serial invocations. Use this to prove the pipeline and to run
the bake-off; use the API path for a backfill.

Cost accounting survives — the CLI reports `total_cost_usd` and full token
counts, so the bake-off's cost column stays meaningful across all three
backends.

### The cost floor

DESIGN §11.5 lists a local LLM as worth benchmarking "purely as a cost floor",
and `--model local:<id>` runs one through LM Studio. Expect it to do worse, for
two structural reasons rather than one:

- **The input is degraded.** Local models can't take PDF blocks, so pages are
  flattened to text — the exact operation that destroys the column alignment you
  need to tell this year from the prior-year comparative. Table detection
  recovers some of it.
- **The context is smaller.** Sixty pages runs past what a 7B model holds, so
  the text is truncated. Truncation drops the notes first, and the notes are
  where contingent liabilities and the auditor's remarks live.

The point of wiring it up is that the validators and the bake-off already exist,
so "how much worse" is a measurement rather than an argument. Run
`bakeoff --models local:<id>` and read the confidence distribution.

## Phase 2: the factor model

Twenty factors across DESIGN §6.1's five families, combined into a 0-100 score
with the §6.2 red-flag overlay on top. See [PHASE2-FINDINGS.md](PHASE2-FINDINGS.md)
for what the first live run measured.

```
raw factor  --sector z-score-->  z  --sign-->  family mean  --standardise-->
weighted sum  --standardise-->  Phi()  -->  0-100  --overlay-->  signal
```

**Four things in that chain are not obvious**, and each changes the answer.

*Signs are normalised before aggregation, not at selection.* Debt/equity and
accruals are lower-is-better. The single-factor engine could defer direction to
`nlargest`/`nsmallest`; a weighted sum cannot, and one that has not flipped them
subtracts quality from quality without raising anything.

*Family scores are re-standardised before weighting.* Averaging six correlated
z-scores gives something whose variance depends on how correlated they happen to
be, not on the weight assigned to it. Without this, the declared 30/10 split
between quality and sentiment is not the split that gets applied.

*The composite is re-standardised again before the normal CDF.* Otherwise the
whole universe compresses into roughly 30-70 and DESIGN's BUY threshold of 75 is
unreachable by anyone. The consequence is that **the score is explicitly
relative**: 75 means "top quartile of this universe on this date", never "cheap".
A universe of uniformly overvalued companies still produces BUYs.

*Missing data reduces coverage; it never scores as neutral.* Below
`--min-coverage` a company gets no score and no signal at all.

### Missing data is the whole problem

Fifteen of the twenty factors read `fundamentals_annual`, which phase 1 has not
backfilled. Measured across the Nifty 100:

| Family | Weight | Factors | With data |
|---|---|---|---|
| Value | 25% | 5 | **0** |
| Quality | 30% | 6 | **0** |
| Growth | 20% | 5 | 2 (quarterly, 85% of companies) |
| Momentum | 15% | 3 | 3 |
| Sentiment | 10% | 1 | **0** |

So most companies top out at **23% coverage**, nothing reaches the default
`min_coverage` of 0.5, and the default configuration scores **zero of 100
companies**. Two guards keep that from becoming a plausible-looking number:

| Guard | What it prevents |
|---|---|
| `min_coverage` withdraws the score | A company ranked on momentum alone competing against one ranked on all five families as though the numbers meant the same thing |
| Red flags are tri-state | `UNKNOWN` never reading as `CLEAR` |

Running the composite today therefore requires saying so explicitly:

```bash
stockanalysis score --min-coverage 0.20   # the default 0.5 scores nothing
```

That is the intended behaviour, not a workaround. The default refuses.

**What is free.** Running `ingest-quarterly` and `ingest-shareholding` across the
full universe rather than at `--limit 3` took quarterly coverage from 3% to 85%
of companies and made the promoter-selling red flag evaluable for all 100 — no
API key, no model, ~200 rate-limited requests. Everything still missing needs
the LLM backfill.

### The result

Nifty 100, Jan 2022 – Aug 2026, top 20, monthly rebalance, costs on, at the 15%
(momentum-only) coverage that covers the whole window:

```
                         composite   EW hold
  CAGR                      19.15%    20.29%
  Sharpe                      0.66      0.88
  Max drawdown             -23.84%   -20.33%
  [-] composite LAGGED equal-weight hold by 1.14%/yr
```

Better than phase 0's single momentum factor (18.50%, lagging by 1.79%) and
still losing to holding the universe. The shuffled-label control — selection
severed from outcome — returns **19.46% / Sharpe 0.71**, slightly *better* than
the real run. No leak, and no edge: the composite's selection is statistically
indistinguishable from picking 20 names at random.

That is a result about momentum on large-cap Indian equities in a bull market,
not about DESIGN's factor model. Value and Quality are 55% of the weight and
have never been measured.

### The red-flag overlay is tri-state, not boolean

DESIGN §6.2 lists six flags. **Two of them no ingested source can evaluate** —
promoter pledge (`NSE.shareholding()` carries no pledged-shares figure) and
credit rating downgrades (nothing ingests ratings). A boolean overlay would
report both as `False`, indistinguishable from a company with a verified zero
pledge, and hand a clean bill of health to precisely the companies the flag
exists to catch. So every flag returns `TRIPPED`, `CLEAR` or `UNKNOWN`, the
unknown list is stored on the signal row, and `score` prints it.

Measured across all 100 companies, **one of the six checks actually runs**:

| Flag | State | Why |
|---|---|---|
| `promoter_selling` | CLEAR 95, **TRIPPED 5** | `NSE.shareholding()`, working |
| `promoter_pledge` | UNKNOWN 100 | No pledged-shares figure anywhere |
| `rating_downgrade` | UNKNOWN 100 | Nothing ingests ratings |
| `auditor_qualification` | UNKNOWN 100 | Needs `fundamentals_annual` |
| `weak_cash_conversion` | UNKNOWN 100 | Needs `fundamentals_annual` |
| `contingent_liabilities` | UNKNOWN 100 | Needs `fundamentals_annual` |

Under a boolean overlay this run would have reported "0 red flags" for 95
companies and read as a clean universe. An unflagged company is not certified;
it is unflagged *on the flags that could be checked*.

### Attribution is how you tell a model from a lucky factor

A backtest reports whether a portfolio worked. With twenty factors and one NAV
curve it cannot tell you which of them was responsible.

```bash
stockanalysis attribution --start 2021-06-30
```

Per factor: mean rank IC against next-period returns, its t-statistic, decile
spread, monotonicity across buckets, and data coverage. Signs are normalised, so
a positive IC always means the factor worked — otherwise debt/equity would report
negative while doing exactly its job. The number that decides whether an IC is
real is the t-statistic, not the mean: an IC of 0.04 over 12 monthly
observations is noise, and the same figure over 120 is a factor.

## Phase 3: news and sentiment

Four RSS feeds, a ticker resolver, FinBERT, and a GDELT backfill. The pipeline
runs end to end and the sentiment factor produces real numbers for the first
time. It is still not backtestable, for a reason worth stating precisely.

### Attribution is the hard part, not scoring

Scoring a headline is a solved problem — a 110M-parameter model does it locally
in milliseconds. Deciding *which company a headline is about* is where a news
factor is won or lost, because an unresolved article costs recall and can be
recovered later from stored text, while a **mis**-resolved one is
indistinguishable from data once it is in the table.

So every rule is biased towards precision, and the ones that matter refuse
things:

- **Group names are never aliases.** "Tata", "Bajaj", "Adani" and "Mahindra"
  each front a dozen listed companies.
- **An alias two companies claim goes to neither.** The build-time conflict
  pass dropped five, including `tata motors` (Tata Motors and Tata Motors
  Passenger Vehicles, post-demerger) and `siemens` (Siemens and Siemens Energy
  India). Both demergers happened inside the backtest window.
- **`hdfc` is blocked outright** — it named HDFC Ltd before July 2023 and HDFC
  Bank after, and aliases here carry no validity dates.
- **A roundup is not company news.** An article naming more than three
  companies is a list, and a document-level sentiment score is only
  attributable when the document is about one thing. This discards a quarter of
  the resolver's output on purpose.
- **Body-only mentions do not count.** Measured on a manual audit: eight of
  nine were about some other company.

```bash
stockanalysis build-aliases     # prints what it refused and why
stockanalysis news-status       # attribution by method, monthly coverage
```

Nothing is thrown away. Unresolved and below-threshold articles are stored with
their text, so improving the alias table is a `reresolve-news` away rather than
another pass over the sources.

### GDELT is the constraint

RSS has no history — a feed carries its current front page, so it builds an
archive going forward and cannot fill one in. GDELT is the only historical
source, and its public API sustains about **12 company-months an hour** against
a documented rate of one request every five seconds. Slowing down does not
help; the 429s arrive at 6s, 12s and 20s spacing alike.

```
  Nifty 100 x 3 years   3,600 windows  ->  ~12 days
  Nifty 100 x 1 year    1,200 windows  ->  ~4 days
```

So `backfill-news` is built to be run overnight and interrupted: every
(company, month) window is checkpointed as it lands, and a throttled window is
left *unrecorded* so the next run retries it rather than checkpointing a
throttle as "no news".

With a partial backfill of 20 companies, the sentiment factor computes for 7 of
100 companies at a November 2025 decision date, and for 1 of 100 today on RSS
alone. That is the honest state: the pipeline works, the history does not exist
yet, and the gap is calendar time. See
[PHASE3-FINDINGS.md](PHASE3-FINDINGS.md).

### The label order that would have inverted everything

FinBERT's `id2label` is `{0: positive, 1: negative, 2: neutral}` — not
alphabetical, and not the negative-first order most classifiers use. Hardcoding
an index inverts the entire factor while the backtest keeps running and every
conclusion comes out backwards. The mapping is read from the model at load
time, a model without explicit polarity labels is refused, and a test runs the
real model over three headlines to check the signs.

## Next: phase 4

Serving — FastAPI endpoints, a Streamlit dashboard, LLM narrative generation
over the existing scores, a daily scheduled run, and alerting on signal changes
and newly tripped red flags. See DESIGN.md §9.
