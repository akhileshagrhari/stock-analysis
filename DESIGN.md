# StockAnalysis — Design Document

**Market:** Indian equities (NSE/BSE)
**Status:** Draft v1 — pre-implementation
**Date:** 2026-08-13

---

## 1. What this system is

A research system that ingests price, fundamental, and news data for Indian listed
companies, converts unstructured annual reports into structured financials, scores
each company against a transparent factor model, and emits a **BUY / HOLD / SELL**
signal with a written rationale and an auditable trail of the numbers behind it.

### What it is not

- **Not an execution system.** No order placement, no broker write access. Read-only
  market data. (This also sidesteps the SEBI static-IP mandate for API-based trading,
  effective 1 April 2026.)
- **Not a black box.** The signal comes from named factors with published weights.
  The LLM writes the explanation; it does not invent the score.
- **Not a distributable advisory product.** In India, issuing buy/sell recommendations
  to other people for consideration requires SEBI Research Analyst registration.
  Built for personal research use. If that changes, the compliance question comes
  before the code.

### The honest constraint

Direct price prediction does not work reliably — this is one of the better-established
results in quantitative finance, and it holds for the current crop of time-series
foundation models too. This design therefore puts ML where it demonstrably earns its
place — **turning 300-page PDFs into numbers**, and **scoring news sentiment** — and
puts the actual buy/sell decision in a rules-based factor model that can be backtested
and explained. Anything that cannot survive a walk-forward backtest does not ship.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  INGEST                                                          │
│  ├── prices      yfinance / broker API  → daily OHLCV, adjusted  │
│  ├── corp data   NseIndiaApi            → shareholding, actions  │
│  ├── filings     NSE/BSE annual reports → PDFs to object store   │
│  └── news        RSS + Marketaux        → headlines, bodies      │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  EXTRACT                                                         │
│  ├── PDF → section locator → Claude structured output → schema   │
│  ├── arithmetic validators + NSE cross-check → confidence score  │
│  └── FinBERT → per-article sentiment → daily aggregate per ticker│
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STORE — DuckDB (point-in-time; every row carries knowledge date)│
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  SCORE                                                           │
│  ├── factor computation → sector-relative z-scores                │
│  ├── composite score 0–100 + red-flag overlay                    │
│  └── LLM narrative generation (reads the score, doesn't set it)  │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
┌──────────────────────────┬──────────────────────────────────────┐
│  BACKTEST (walk-forward) │  SERVE (FastAPI + dashboard)          │
└──────────────────────────┴──────────────────────────────────────┘
```

---

## 3. Data sources

### 3.1 Prices

| Source | Role | Notes |
|---|---|---|
| `yfinance` | Primary for v1 | `RELIANCE.NS`, `.BO`. Free, unofficial, no key. Adequate for EOD backtesting. Will break without warning — wrap it. |
| Angel One SmartAPI / Upstox | Upgrade path | Free tiers, genuine realtime. Upstox's **Analytics Token** avoids daily re-auth, which matters for an unattended job. |
| Twelve Data | Fallback | 800 req/day free. |

**Decision:** build a `PriceProvider` interface with a yfinance implementation first.
Broker APIs slot in behind the same interface when EOD is no longer enough.

**Corporate actions are not optional.** Splits and bonuses in Indian markets are
frequent; an unadjusted series produces fake 50% drawdowns that will poison every
momentum factor. Store both raw and adjusted close, and reconcile against
`NSE.actions()`.

### 3.2 Filings and fundamentals

The hard part, and where the system's edge lives.

- **[NseIndiaApi](https://github.com/BennyThadikaran/NseIndiaApi)** —
  `NSE.annual_reports(symbol)` returns annual report PDFs by year, fetched with
  `NSE.download_document()`. This is the single most valuable API call in the project.
- `NSE.results_comparison(symbol)` — quarterly revenue / net profit / EPS for roughly
  the last 5 quarters. **Amounts are in ₹ lakhs.** Doubles as free ground truth to
  validate LLM extraction against.
- `NSE.shareholding(symbol)` — quarterly shareholding pattern. Source for promoter
  holding and pledge tracking.
- `NSE.announcements()`, `NSE.boardMeetings()`, `NSE.bulkdeals()` — event stream.
- **[BseIndiaApi](https://github.com/BennyThadikaran/BseIndiaApi)** — fallback for
  BSE-only listings.

**Rate discipline is mandatory.** These are unofficial wrappers over a public site.
0.5–1s sleep between requests, bulk downloads after market hours, a persistent
on-disk cache so a re-run never re-fetches, and exponential backoff. Getting the
IP blocked is the main operational risk in phase 1.

### 3.3 News

| Source | Why |
|---|---|
| RSS (Moneycontrol, Economic Times Markets, Livemint, Business Standard) | Free, unlimited, best India coverage. The backbone. |
| Marketaux | Entity-tagged — it knows which ticker an article is about, which saves building a resolver. Free tier. |
| GDELT | Free and unlimited historical archive. Needed to backtest the sentiment factor, which RSS cannot do (no history). |

Ticker resolution from headline text is its own small problem (`Reliance` → RELIANCE
vs Reliance Power vs Reliance Infra). Start with an alias table plus exchange-symbol
matching; escalate to an LLM resolver only if precision is unacceptable.

---

## 4. Data model

DuckDB. Single-file, columnar, fast analytical scans, zero server. Migrate to
Postgres + TimescaleDB only if concurrent writers appear.

```sql
instruments(isin PK, nse_symbol, bse_code, name, sector, industry,
            listing_date, delisting_date, is_active)

prices_daily(isin, date, open, high, low, close, adj_close, volume,
             delivery_pct, PRIMARY KEY(isin, date))

corporate_actions(isin, ex_date, action_type, ratio, details)

filings(filing_id PK, isin, doc_type, fiscal_year, period_end,
        broadcast_date, source_url, local_path, sha256)

fundamentals_annual(isin, fiscal_year, period_end_date,
                    filing_date,          -- when it became public
                    revenue, ebitda, pat, eps, ocf, fcf, capex,
                    total_assets, total_equity, total_debt, cash,
                    interest_expense, tax_expense, contingent_liabilities,
                    auditor_opinion, extraction_confidence, source_filing_id)

fundamentals_quarterly(...)              -- from NSE.results_comparison

shareholding(isin, quarter_end, promoter_pct, promoter_pledged_pct,
             fii_pct, dii_pct, public_pct)

news(news_id PK, isin, published_at, ingested_at, headline, body,
     source, url)

news_sentiment(news_id, model, label, score, computed_at)

factor_scores(isin, as_of_date, factor_name, raw_value,
              sector_zscore, PRIMARY KEY(isin, as_of_date, factor_name))

signals(isin, as_of_date, composite_score, signal, red_flags,
        narrative, model_version)

backtest_runs(run_id, config_json, started_at, metrics_json)
```

### 4.1 Point-in-time correctness — the thing that decides whether this works

Two failure modes destroy backtests, silently, and both are the default behaviour if
you do nothing:

**Lookahead bias.** FY2024 annual report figures were not knowable in April 2024;
the report was published in July. Every fundamental row therefore carries **both**
`period_end_date` (what it describes) and `filing_date` (when it became public). The
backtest may only read rows where `filing_date <= decision_date`. This is not a
refinement to add later — retrofitting it means rerunning everything and usually
discovering the strategy never worked.

**Survivorship bias.** A universe built from *today's* Nifty 500 has quietly deleted
every company that collapsed. Backtested returns come out beautiful and are fiction.
The `instruments` table keeps delisted companies with a `delisting_date`, and the
universe is reconstructed as of each rebalance date.

Both get an explicit test in the test suite, not a comment in the code.

---

## 5. LLM extraction pipeline

### 5.1 The problem

Indian annual reports are PDFs, not XBRL. A typical one is 200–400 pages: chairman's
letter, ESG narrative, directors' report, MD&A, then the financial statements and
notes. There is no consistent layout across companies or across years for the same
company.

### 5.2 Pipeline

```
PDF (200–400pp)
  │
  ├─ 1. Section locator ─── PyMuPDF text extraction + regex/heading search
  │                         for "Balance Sheet" / "Statement of Profit and Loss" /
  │                         "Cash Flow Statement" / "Notes to Financial Statements"
  │                         → narrows to ~40–80 relevant pages
  │
  ├─ 2. Structured extraction ─ Claude (claude-opus-5) with a Pydantic schema via
  │                             client.messages.parse(). PDF pages passed as
  │                             document content blocks. Adaptive thinking on,
  │                             effort=high — this is a precision task.
  │
  ├─ 3. Validation ────────── arithmetic identities:
  │                             assets == liabilities + equity      (±0.5%)
  │                             revenue − expenses ≈ PAT            (±2%)
  │                             OCF − capex == FCF
  │                           cross-check vs NSE.results_comparison()
  │                           (remember: NSE returns ₹ lakhs)
  │
  ├─ 4. Confidence scoring ── all checks pass          → 1.0, auto-accept
  │                           one soft check fails      → 0.6, flag
  │                           identity fails / missing  → 0.0, human review queue
  │
  └─ 5. Persist ───────────── fundamentals_annual + link to source_filing_id
```

**Why an LLM rather than table parsers.** Camelot/Tabula extract *a* table; they
cannot tell you that this particular table is the consolidated balance sheet rather
than the standalone one, that the comparative column is the prior year, or that
"Revenue from Operations" and "Total Income" are different line items. That
judgement is exactly what a model is for. Deterministic validators then catch its
mistakes — the two halves are complementary, and neither is sufficient alone.

### 5.3 Key API details

- **PDF input limits:** 32MB per request, 600 pages (100 on 200K-context models).
  Some Indian annual reports exceed 32MB — hence the section-locator step, which
  also cuts cost by ~4×.
- **Structured outputs:** `output_config.format` with a JSON schema, or
  `client.messages.parse()` with a Pydantic model (preferred — it validates the
  response automatically). Note the schema restrictions: no recursive schemas, no
  numeric constraints like `minimum`/`maximum`. Range checks belong in step 3, not
  the schema.
- **Prompt caching:** the extraction system prompt plus the schema is a large stable
  prefix reused across every report. Put a `cache_control` breakpoint at the end of
  it. Cache reads cost ~0.1× base input. Minimum cacheable prefix on `claude-opus-5`
  is 512 tokens — well under our system prompt size.
- **Batch API:** backfilling five years of history is not latency-sensitive. Submit
  as batches for **50% off**. Results come back keyed by `custom_id`, in arbitrary
  order — key by `custom_id`, never by position.
- **Files API:** upload each PDF once and reference by `file_id` if a report needs
  multiple extraction passes.

### 5.4 Model selection and cost

Current pricing, per million tokens:

| Model | ID | Input | Output | Context |
|---|---|---|---|---|
| Claude Opus 5 | `claude-opus-5` | $5.00 | $25.00 | 1M |
| Claude Sonnet 5 | `claude-sonnet-5` | $3.00 (**$2.00 intro through 2026-08-31**) | $15.00 ($10.00 intro) | 1M |
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1.00 | $5.00 | 200K |

**Working cost estimate** — stated assumptions, not measured: a PDF page runs roughly
1,500–3,000 tokens. After the section locator narrows to ~60 pages, that is
~120k–180k input tokens per report, with negligible output (a JSON object).

| Scenario | Per report | Nifty 100 × 3yr (300 reports) | Nifty 500 × 5yr (2,500 reports) |
|---|---|---|---|
| Opus 5, no batch | ~$0.75 | ~$225 | ~$1,875 |
| Opus 5 + batch (50%) | ~$0.38 | ~$115 | ~$940 |
| Sonnet 5 + batch, intro pricing | ~$0.15 | ~$45 | ~$375 |

**Recommendation:** run a **10-report bake-off** — Opus 5 against Sonnet 5, scored on
validator pass rate and agreement with `NSE.results_comparison()`. Extraction accuracy
compounds into every downstream factor, so buy accuracy where it is cheap relative to
the alternative (silent data corruption). Start the backfill scoped to **Nifty 100,
3 years** — ~$45–115 is a sane number to spend before the system has proven anything.

### 5.5 Local models

| Task | Model | Runs on |
|---|---|---|
| News sentiment | [ProsusAI/finbert](https://huggingface.co/ProsusAI/finbert) | CPU, 110M params |
| Sentiment (alt) | [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) v3 LoRA | single RTX 3090 |

FinBERT runs locally over thousands of headlines a day for free. Using a frontier API
model for per-headline sentiment classification would be strictly worse economics for
no measurable accuracy gain.

**Time-series foundation models — evaluate, do not build on.** Chronos-2 (Amazon,
9M–710M params, best-documented), TimesFM (Google), Moirai-2, and **Kronos**
(pretrained on OHLCV candlesticks from 45+ exchanges — the most directly relevant)
are all open-weight and worth benchmarking against a naive random-walk baseline.
Treat any of them as a *candidate factor* that must earn its slot in the backtest,
never as the signal itself.

---

## 6. Factor model

Scores are computed **sector-relative**. A P/E of 25 is expensive for a PSU bank and
cheap for an FMCG company; absolute cross-sector comparison is meaningless. Within
each sector, each factor is converted to a z-score, winsorised at ±3σ.

### 6.1 Factor families

**Value (25%)** — earnings yield (E/P), P/B, EV/EBITDA, FCF yield, PEG

**Quality (30%)** — ROE, ROCE, debt/equity, interest coverage, and two India-specific
earnings-quality checks that carry disproportionate weight:
- **CFO/PAT ratio.** Persistent operating cash flow far below reported profit is the
  single most reliable published warning sign in Indian mid-caps.
- **Accruals ratio** — (PAT − OCF) / total assets.

**Growth (20%)** — revenue CAGR 3yr, PAT CAGR 3yr, latest YoY quarterly growth,
margin trend

**Momentum (15%)** — 12-month-minus-1-month return, price vs 200-DMA, relative
strength vs Nifty 500

**Sentiment (10%)** — 30-day FinBERT-weighted news score, announcement-event flags

### 6.2 Red-flag overlay

These do not adjust the score — they **cap** it. A company tripping any of these
cannot be rated BUY regardless of how good its factors look:

- Promoter pledge > 25% of promoter holding, or rising sharply quarter-on-quarter
- Auditor qualification, adverse opinion, or auditor resignation
- CFO/PAT < 0.5 sustained across three consecutive years
- Promoter holding falling for three consecutive quarters
- Contingent liabilities > 50% of net worth
- Any credit rating downgrade

This overlay exists because these signals are cheap to compute, historically
informative in Indian markets, and *not* well captured by a smooth linear factor
score. Most Indian mid-cap blowups were visible in this list before the price moved.

### 6.3 Signal mapping

| Composite | Signal |
|---|---|
| ≥ 75 and no red flags | BUY |
| 45 – 74 | HOLD |
| < 45, or any red flag | SELL / AVOID |

Thresholds are a starting point to be tuned against the backtest — not a claim.

### 6.4 Narrative generation

Once the score exists, a single Claude call receives the factor breakdown, red flags,
and recent news, and writes a paragraph explaining the rating. It receives the score
as input and is instructed not to contradict it. The signal is deterministic and
reproducible; only the prose is generated.

---

## 7. Backtest

**Build this before the recommender.** Without it, there is no way to distinguish a
working factor model from an elaborate random number generator.

- **Method:** walk-forward. Train/tune on an expanding window, evaluate strictly
  out-of-sample. No cross-validation across time — it leaks the future.
- **Rebalance:** monthly.
- **Universe:** reconstructed as of each rebalance date from `instruments`, including
  companies that later delisted.
- **Costs modelled:** brokerage, STT, exchange fees, stamp duty, GST, plus a slippage
  assumption scaled by the stock's median traded value. Ignoring costs is the second
  most common way to produce a backtest that cannot be traded.
- **Benchmark:** Nifty 500 TRI (total return, so dividends are counted on both sides).
- **Metrics:** CAGR, volatility, Sharpe, Sortino, max drawdown, hit rate, turnover,
  factor attribution, decile spread.
- **Sanity check:** run the identical pipeline on shuffled labels. If it still shows
  alpha, there is a leak. Find it before believing anything else.

---

## 8. Tech stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Ecosystem |
| Store | DuckDB | Single-file, columnar, fast scans, no server |
| Dataframes | Polars (ingest), pandas (factors) | Polars for speed; pandas where the finance libs expect it |
| LLM | `anthropic` SDK | `messages.parse()` + Pydantic for extraction |
| Local ML | `transformers`, `torch` | FinBERT |
| PDF | PyMuPDF | Section location and text extraction |
| Scheduling | APScheduler → Prefect | Start simple |
| Backtest | Custom harness + `vectorbt` | Point-in-time correctness is easier to guarantee in owned code |
| API | FastAPI | |
| UI | Streamlit (v1) → Next.js | Streamlit gets a usable dashboard in a day |
| Config | Pydantic Settings + `.env` | |
| Tests | pytest | Lookahead + survivorship tests are non-negotiable |

---

## 9. Roadmap

### Phase 0 — Spine (week 1–2)
Repo scaffold, config, DuckDB schema, `instruments` seeded with Nifty 100 including
historical constituents. Price ingest via yfinance with corporate-action adjustment.
**A working backtest harness driven by one deliberately dumb factor (12-1 momentum).**
Lookahead and survivorship tests written and passing.

*Exit criterion: a backtest that runs end-to-end and reports a plausible, unimpressive
Sharpe. If momentum shows a Sharpe of 4, there is a bug — find it now, not in phase 3.*

### Phase 1 — Extraction (week 3–5)
Annual report downloader with caching and rate limiting. Section locator. Claude
extraction with schema and validators. **10-report model bake-off (Opus 5 vs Sonnet 5).**
Backfill Nifty 100 × 3 years via Batch API. Human review queue for low-confidence rows.

*Exit criterion: ≥95% of extractions pass arithmetic validation and agree with NSE
quarterly data within tolerance.*

### Phase 2 — Factors (week 6–7)
All five factor families. Sector-relative z-scoring. Red-flag overlay. Composite score.
Full backtest with transaction costs. Factor attribution.

*Exit criterion: an honest performance report — including the possibility that it does
not beat the index, which is a legitimate and useful finding.*

### Phase 3 — News & sentiment (week 8)
RSS + Marketaux ingest, ticker resolution, FinBERT scoring, GDELT historical backfill
so the sentiment factor can be backtested rather than assumed.

### Phase 4 — Serving (week 9–10)
FastAPI endpoints, Streamlit dashboard, LLM narrative generation, daily scheduled run,
alerting on signal changes and newly tripped red flags.

### Phase 5 — Extension (optional)
Kronos / Chronos-2 benchmarking as candidate factors. Multi-market via a second
adapter behind the existing interfaces — the schema is already market-agnostic, and
SEC EDGAR's free XBRL API would make US coverage substantially easier than India was.

---

## 10. Principal risks

| Risk | Mitigation |
|---|---|
| NSE/BSE unofficial APIs break or IP-block | Aggressive caching, rate limiting, BSE fallback, never re-fetch what is on disk |
| LLM extraction errors propagate silently into every factor | Arithmetic validators + NSE cross-check + confidence scoring + human review queue |
| Backtest looks great, is leaking | Explicit lookahead/survivorship tests; shuffled-label control run |
| Factor model does not beat the index | Legitimate outcome. The system's value is then the extraction and screening layer, which stands on its own |
| Extraction cost overruns | Section locator (~4× reduction), Batch API (50%), prompt caching, phased universe |
| Sonnet 5 intro pricing ends 2026-08-31 | 18 days out. Run the bake-off soon if intro pricing affects the model choice |

---

## 11. Open decisions

1. **Universe size for v1** — Nifty 100 (recommended: cheap, clean, liquid) vs Nifty 500?
2. **Consolidated vs standalone financials** — recommend consolidated where available,
   with an explicit flag on the row.
3. **Extraction model** — resolve via the phase-1 bake-off, not by argument.
4. **Rebalance frequency** — monthly assumed; test quarterly as a lower-turnover variant.
5. **Local LLM fallback** (Qwen/Llama via Ollama) for extraction — worth benchmarking
   in phase 1 purely as a cost floor, given the 2,500-report backfill scenario.

---

## Sources

- [NSE corporate filings — annual reports](https://www.nseindia.com/companies-listing/corporate-filings-annual-reports)
- [NseIndiaApi](https://github.com/BennyThadikaran/NseIndiaApi) · [API reference](https://bennythadikaran.github.io/NseIndiaApi/api.html)
- [BseIndiaApi](https://github.com/BennyThadikaran/BseIndiaApi)
- [Upstox Developer API](https://upstox.com/developer/api-documentation/api-overview/) · [Angel One SmartAPI review](https://www.chittorgarh.com/broker/angel-broking/api-for-algo-trading-review/14/)
- [Twelve Data](https://twelvedata.com/stocks) · [Finnhub](https://finnhub.io/) · [Marketaux](https://www.marketaux.com/)
- [ProsusAI/finbert](https://huggingface.co/ProsusAI/finbert) · [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT)
- [Time series foundation models, 2026](https://machinelearningmastery.com/the-2026-time-series-toolkit-5-foundation-models-for-autonomous-forecasting/)
- Anthropic API: model IDs, pricing, structured outputs, prompt caching, Batch API