# Phase 2 — findings from building and running the factor model

**Date:** 2026-08-13
**Scope:** DESIGN §6 — all five factor families, sector-relative z-scoring, the
red-flag overlay, composite scoring, factor attribution, and a backtest of the
composite against the equal-weight benchmark.

**Result:** 2 bugs found and fixed, both of the silent kind. The headline
measurement is that **the model runs at 15% data coverage**, because fifteen of
its twenty factors read fundamentals phase 1 has not backfilled — and the work
that mattered most was making the system say so rather than produce a number
anyway.

Test suite went 174 → 209.

---

## 1. Bugs fixed

### 1.1 A factor with no data was scored as "average" for every company

**Severity:** critical — it made every subsequent measurement meaningless.
**Where:** `factors/base.py`, `sector_zscore`
**Introduced:** phase 0. Invisible until phase 2.

`sector_zscore` falls back to 0.0 when a sector has no usable spread:

```python
if not np.isfinite(sigma) or sigma == 0:
    out.loc[list(idx)] = 0.0
```

For a factor with data, that is right — a sector where every company reports the
same number genuinely has all of them at its middle. But the branch is also
reached when the sector has **no data at all**, in which case `sigma` is NaN and
every company is assigned a z-score of 0.0.

The first live run of `score` reported:

```
universe 100   scored 100   median coverage 92%
families with data: growth 100%, momentum 100%, quality 100%, sentiment 100%,
value 100%
```

with **zero rows in `fundamentals_annual` and zero rows in `news`**. Every
company had been assigned a neutral score on all five families, the composite
weighted those neutral scores as though they were measurements, and the coverage
metric — the thing built specifically to detect this — was computed downstream
of the same bug and reported 92%.

Nothing in phase 0 or 1 could have caught it. Momentum always has data, so the
branch was never reached with an empty input.

**Why it matters more than a wrong number.** "We could not measure this" and
"this is average" are different claims, and the second one is the more dangerous
because it is actionable. A company with no news coverage would have received an
average sentiment score, diluting the factors that *were* measured, and the
resulting BUY would have looked exactly like a well-supported one.

**Fix:** the 0.0 fallback now applies only where the input is present.

```python
out.loc[members] = present.loc[members].map({True: 0.0, False: np.nan})
```

**Tests:**
- `test_a_factor_with_no_data_does_not_score_as_average` — the regression.
- `test_present_values_still_score_zero_in_a_degenerate_sector` — guards the fix
  from over-correcting and deleting a whole sector that legitimately has no
  spread.

**After the fix**, the same command reports what is actually there:

```
universe 100   scored 0   median coverage 15%
families with data: growth 3%, momentum 100%, quality 0%, sentiment 0%, value 0%
```

Zero scored is the correct answer at the default coverage threshold.

---

### 1.2 NSE files the same quarter twice, sometimes in different units

**Severity:** high — a 50/50 chance of a silent 100x error, plus a hard crash.
**Where:** `ingest/nse_fundamentals.py`, `parse_results_comparison`
**Found by:** running `ingest-quarterly` across the full Nifty 100 rather than
the `--limit 3` used in phase 1.

`ingest-quarterly` aborted on:

```
ConstraintException: PRIMARY KEY or UNIQUE constraint violation:
duplicate key "INE129A01019, 2024-12-31"
```

GAIL's December 2024 quarter comes back twice:

| `re_seq_num` | `re_total_inc` | `re_net_profit` |
|---|---|---|
| 1191981 | 3,570,747 | 386,738 |
| 1191533 | 35,707.47 | 3,867.38 |

Exactly 100x apart. `re_res_type` (`U`), `re_face_val` (`10`) and every other
field are identical. **Nothing in the payload declares the unit.** The module's
documented "amounts are in rupees lakhs" assumption is correct for the great
majority of rows and wrong for one of these two.

The crash is the benign half of this bug. The dangerous half is that both rows
get multiplied by `LAKH_TO_CRORE` regardless of which unit they were actually
in, so whichever survived a deduplication-by-position would have had a 50%
chance of understating revenue and PAT by 100x — in the table that exists to
*cross-check the LLM extraction for exactly this class of error*.

A second variety showed up too: ONGC's March 2024 quarter filed twice with
identical values. Benign in content, still fatal to the primary key.

**Fix:** resolve by internal consistency rather than by picking a row. A
company's revenue does not move by two orders of magnitude between quarters, so
the quarters that came back unambiguously establish the scale, and the duplicate
closest to it in log space is the one denominated the same way. With no
unambiguous quarter to calibrate against, the period is dropped — a missing
quarter costs coverage, a 100x error corrupts everything downstream of it.

**Tests:** `test_a_quarter_filed_twice_in_different_units_yields_one_row`,
`test_the_duplicate_matching_the_companys_own_scale_is_kept`,
`test_an_uncalibratable_duplicate_is_dropped_rather_than_guessed`.

**Incidence:** 2 affected companies in the Nifty 100 (GAIL, ONGC) — rare enough
to have been missed at `--limit 3`, common enough to abort a full ingest.

---

## 2. Coverage is the binding constraint, not the factor design

All twenty factors are implemented. **Five of them have data.**

| Family | Weight | Factors | With data | Coverage |
|---|---|---|---|---|
| Value | 25% | 5 | 0 | 0% |
| Quality | 30% | 6 | 0 | 0% |
| Growth | 20% | 5 | 2 | 85% of companies |
| Momentum | 15% | 3 | 3 | 100% |
| Sentiment | 10% | 1 | 0 | 0% |

Weight-weighted coverage across the Nifty 100 on 2026-07-31:

```
0.23  77 companies      momentum + both quarterly growth factors
0.19   8 companies      momentum + one quarterly factor
0.15  13 companies      momentum only
0.10   2 companies      partial momentum
```

**Nothing reaches the default `min_coverage` of 0.5**, so the default
configuration scores zero companies out of 100. Running the composite at all
requires passing `--min-coverage 0.20` and accepting what that means: the model
being evaluated is not DESIGN §6's model, it is its momentum-and-quarterly-growth
quarter.

Everything missing traces to one place — `fundamentals_annual` has zero rows
because the phase-1 backfill is blocked on the NCI validator issue
(PHASE1-FINDINGS §2.1). Value, Quality, three of five Growth factors, and four
of six red flags all read that table.

### What the free NSE sources bought

Phase 1 ran `ingest-quarterly` and `ingest-shareholding` at `--limit 3`. Running
them across the full universe was the cheapest coverage available and worth
recording separately from the LLM path:

| | Before | After |
|---|---|---|
| `fundamentals_quarterly` | 15 rows, 3 companies | **457 rows, 93 companies** with ≥2 quarters |
| `shareholding` | 63 rows | **2,020 rows**, all 100 companies with ≥4 quarters |
| Quarterly rows with a real NSE broadcast date | 15 | **452 of 457** |

That is what took growth from 3% to 85% of companies, and it is what makes the
promoter-selling red flag evaluable for the entire universe. No API key, no
model, ~200 rate-limited requests.

---

## 3. The red-flag overlay is 1/6 operational

DESIGN §6.2 lists six flags. Measured across all 100 companies:

| Flag | State | Why |
|---|---|---|
| `promoter_selling` | **CLEAR 95, TRIPPED 5** | `NSE.shareholding()`, working |
| `promoter_pledge` | UNKNOWN 100 | No pledged-shares figure in any ingested source |
| `rating_downgrade` | UNKNOWN 100 | Nothing ingests credit ratings |
| `auditor_qualification` | UNKNOWN 100 | Needs `fundamentals_annual` |
| `weak_cash_conversion` | UNKNOWN 100 | Needs `fundamentals_annual` |
| `contingent_liabilities` | UNKNOWN 100 | Needs `fundamentals_annual` |

This is the number the tri-state exists to produce. Under a boolean overlay the
same run would have reported **"0 red flags"** for 95 companies and read as a
clean universe. What it actually means is that one check out of six ran.

Five companies genuinely trip promoter-selling and are correctly capped to SELL,
so the overlay is not merely wired — it fires.

---

## 4. Performance — the honest report DESIGN §9 asks for

Nifty 100, monthly rebalance, top 20, transaction costs on, benchmarked against
equal-weight buy-and-hold of the same universe.

### 4.1 Composite at 15% coverage (momentum only), Jan 2022 – Jul 2026

```
                         composite   EW hold
  CAGR                      19.15%    20.29%
  Volatility                19.27%    15.69%
  Sharpe                      0.66      0.88
  Max drawdown             -23.84%   -20.33%
  [-] composite LAGGED equal-weight hold by 1.14%/yr
```

Marginally better than phase 0's single 12-1 momentum factor (18.50% CAGR,
lagging by 1.79%/yr) — blending three momentum factors beats one — and still
losing to simply holding the universe.

### 4.2 Composite at 20% coverage (adds quarterly growth)

```
  Periods                     17  (1.42 years)
  CAGR                      16.56%    20.29%
  Sharpe                      0.63      0.88
  [-] composite LAGGED equal-weight hold by 3.73%/yr

  WARNING  36/54 rebalances skipped for insufficient data
```

**Read the warning, not the CAGR.** `NSE.results_comparison` returns only about
five recent quarters, so once the knowledge-date filter is applied there is no
quarterly data at all before mid-2024. Thirty-six of fifty-four rebalances
produced no portfolio, and the surviving 1.42 years is too short to mean
anything. This row is in the report because deleting an unflattering run with a
legitimate explanation is how backtests become fiction.

### 4.3 Shuffled-label control — DESIGN §7's sanity check

Selection severed from outcome by drawing each holding's return at random from
the same date's universe:

| | CAGR | Sharpe |
|---|---|---|
| Composite (real) | 19.15% | 0.66 |
| Composite (shuffled) | **19.46%** | **0.71** |

**The control does slightly better than the real thing.** No leak — and no edge.
The composite's selection is statistically indistinguishable from picking 20
names at random out of the Nifty 100.

One methodological note worth recording: the first attempt at this control
permuted returns *within the selected portfolio*, which for an equal-weighted
book is an identity operation — real and shuffled came back byte-identical at
19.15%/0.66. `tests/test_control_runs.py:44-49` already documents that trap and
draws from the universe pool instead. The lesson is that a control run producing
suspiciously clean agreement is more likely broken than confirmatory.

### 4.4 Factor attribution

Rank IC against next-month returns, June 2021 – July 2026, signs normalised so
positive always means the factor worked:

```
  factor                family       n   cov     IC     t   hit spread/yr mono
  ----------------------------------------------------------------------------
  quarterly_pat_yoy     growth      18   25%  0.034  1.94   67%     13.2%   no
  quarterly_revenue_yoy growth      18   23%  0.032  1.32   61%     23.7%   no
  relative_strength_6m  momentum    61   96%  0.007  0.37   52%      3.5%   no
  momentum_12_1         momentum    59   92%  0.005  0.26   54%     15.8%   no
  price_to_200dma       momentum    61   96%  0.004  0.19   52%      4.3%   no
```

The remaining 15 factors report `n = 0`.

**No factor here has evidence behind it.** The three momentum factors have ICs
of 0.004–0.007 with t-statistics under 0.4 — indistinguishable from zero, and
consistent with §4.3's shuffled control. The two quarterly growth factors look
better and are not: `quarterly_pat_yoy` at t = 1.94 is under the conventional
bar on 18 observations, and those 18 months are a single recent window whose
data is, per PHASE1-FINDINGS §3.2, materially stale. Treat it as a hypothesis
worth re-testing after the backfill, not a finding.

None of the five is monotonic across deciles, which is a further reason not to
read the spread column as tradeable.

---

## 5. What this means

DESIGN §9's phase-2 exit criterion is "an honest performance report — including
the possibility that it does not beat the index, which is a legitimate and
useful finding". Two findings, and they are different in kind:

1. **The quarter of the model that can be measured does not beat the index.**
   Momentum-family factors show no predictive power on the Nifty 100 over this
   window, on three independent measures — benchmark comparison, rank IC, and a
   shuffled control. That is a real result, and it is what the published
   literature would predict for large-cap momentum over a strong bull market.

2. **The three quarters of the model that DESIGN expects to carry the signal
   have never been measured.** Value and Quality — 55% of the weight, and the
   home of the CFO/PAT and accruals checks that DESIGN calls the most reliable
   published warning signs in Indian mid-caps — have zero coverage.

The second is the more important. Nothing in §4 is evidence about the factor
model DESIGN describes, and the sensible reading of phase 2 is that it built the
apparatus and demonstrated the apparatus is honest about what it does not know.

---

## 6. Recommended order of work

Unchanged in substance from PHASE1-FINDINGS §5, and phase 2 has now measured the
cost of the delay.

1. **Resolve the NCI validator (PHASE1-FINDINGS §2.1).** It blocks the bake-off,
   which blocks the model choice, which blocks the backfill, which blocks 55% of
   the factor weight and four of six red flags.
2. **Run the Nifty 100 × 3yr backfill.** Then re-run `attribution` — value and
   quality either earn their weight or they do not, and that is the first
   question phase 2 can actually answer.
3. Find a promoter-pledge source (SAST/encumbrance filings). It is the single
   most informative red flag in DESIGN's list and currently UNKNOWN for every
   company.
4. Historical index membership, so absolute performance stops being unusable.
   Not a code change — see the README's survivorship note.
5. Phase 3 (news + sentiment) is the *least* urgent of these at 10% weight,
   despite being next in the roadmap.

---

## 7. Files changed

| File | Change |
|---|---|
| `factors/base.py` | `sector_zscore` missing-data fix; `PanelFactor`, `family`, `needs_sector_zscore`, `safe_divide` |
| `factors/panel.py` | **new** — point-in-time panel, implied share count, market cap |
| `factors/value.py` | **new** — 5 factors, all yield-side up |
| `factors/quality.py` | **new** — 6 factors, CFO/PAT and accruals overweighted |
| `factors/growth.py` | **new** — 5 factors, CAGR guards |
| `factors/momentum.py` | +2 factors (200-DMA, relative strength), family tag |
| `factors/sentiment.py` | **new** — wired for phase 3 |
| `factors/redflags.py` | **new** — tri-state overlay |
| `factors/composite.py` | **new** — weights, coverage rule, 0-100 score, signal, persistence |
| `backtest/attribution.py` | **new** — rank IC, decile spread, scipy-free Spearman |
| `backtest/engine.py` | `needs_sector_zscore` bypass, partial-coverage warning |
| `ingest/nse_fundamentals.py` | duplicate-quarter unit resolution |
| `db/database.py` | `as_of_fundamentals_history`, `as_of_quarterly`, `as_of_sentiment`, `as_of_shareholding_history` |
| `db/schema.sql` | `signals.unknown_flags`, `signals.coverage`, two indexes |
| `cli.py` | `score`, `attribution`, `backtest --factor/--min-coverage/--no-red-flags` |
| `tests/test_factor_model.py` | **new** — 32 tests |
| `tests/test_free_nse_sources.py` | +3 duplicate-quarter tests |
| `tests/conftest.py` | `make_fundamentals`, `make_shareholding` |
