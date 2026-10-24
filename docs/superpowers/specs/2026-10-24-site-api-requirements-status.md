# API requirements from the public site — status

**For:** the public-site UI session · **From:** the backend session · **Date:** 2026-10-24
**Spec:** `2026-10-24-site-api-requirements.md` (on `claude/ui-design-c663e2`) · **Branch:** `claude/site-api-requirements-9e3c55`, not pushed

Every item in the spec is done except two parts of R13 (`listing_date`, and the "later" profile fields), which are deferred below with the reason. There is one commit per requirement or data fix. Everything is additive in `/v1`: no field was renamed, removed or retyped, and every new route has parity URLs in `tests/test_postgres_serving.py::PARITY_URLS`.

**Regenerate the site's types** after merging: `stockanalysis export-openapi web/openapi.json`. This session did not write into `web/`. Units and null meanings are in the field descriptions, so they reach the generated types.

| Item | Status | Commit(s) |
|---|---|---|
| D1 (a) stale scoring | done | `204956f`, `0258310` |
| D1 (b) bonus/split share basis | done | `6c19f05`, `a282e7e`, `b6fb79a`, `b12496f`, `a0e18ca`, `3366e92` |
| D2 partial runs stored as full dates | done (pipeline side); API side is R4 | `204956f`, `0258310` |
| D3 coverage 0.23 | resolved by the re-score (see "Real data") | — |
| D4 `close` documented as the traded price | done | `6c19f05` |
| D5 news tagging | done as R12 | `b93d6b3` |
| R1 explanation for a past date | done | `471432f` |
| R2 why a company isn't scored | done | `2d1f954` |
| R3 model catalogue | done | `6f4b7a1` |
| R4 signals per instrument | done | `0c1b92a` |
| R5 previous score on every row | done | `695635c` |
| R6 shareholding pattern | done | `a42c3bb` |
| R7 key metrics | done | `8dc2fd4` |
| R8 corporate actions, adjusted EPS | done | `92c83d2` |
| R9 richer quarterly results | done | `a9bfd23` |
| R10 filing links | done (on the row, not via `filings`) | `2fd384c` |
| R11 per-family history | done | `5ca0d06` |
| R12 news relevance and status | done (no `failed` status; see notes) | `b93d6b3` |
| R13 profile fields | `industry` and `bse_code` done; `listing_date` and the "later" fields deferred | `331708f` |
| R14 freshness in `/v1/health` | done | `e0df940` |
| R15 score on search results | done | `a8176da` |
| R16 sector context per factor | done | `0066da0` |
| R17 units in the financials schema | done | `4f913c1` |
| Site review B1/B2: coverage and contribution described | done | `2668b49` |

**B1/B2** (from the site review): the OpenAPI now says `coverage` is the share of model *weight* backed by data, not of factors (`score-status` has `factors_measured`/`factors_total`). It also says a family's `contribution` is z × weight ÷ (sum of the weights of the families measured for that company), so the page can reproduce it.

---

## Data issues

**D1 (a): why the 24 Oct run was wrong.** It was a one-company job for ADANIENSOL. A company job still scores the whole universe (scoring is sector-relative), but it fetched prices for ADANIENSOL only. Every other company's prices stopped at 11–14 Aug. So the value factors could be computed for one company, and `price_to_200dma` and relative strength read a ten-week-old close as current. Now:
- **Price factors** use the same 45-day tolerance as the value factors.
- **The score step** warns when it scores companies on stale prices, naming the job's scope.
- **Re-scoring a date** replaces what an earlier run stored for it. Before, a company the new run could not score kept the earlier run's signal for that day.

**D1 (b): one share basis.** The stored `close` is Yahoo's, re-based at fetch time across every split and bonus (D4). Per-share figures from a filing are now restated onto the same basis:
- **The restatement factor** comes from NSE's corporate actions (new step `corporate-actions`, checked against the price series). It is the product of the split/bonus ratios with an ex-date after the filing.
- **The share count** is the one each filing states (paid-up capital ÷ face value). PAT ÷ EPS is the fallback.

On the re-scored real data, at the 23 Oct close of ₹1,190.0, RELIANCE's earnings yield is FY2026 EPS 59.69 ÷ ₹1,190.0 = 0.0502, and book-to-price is ₹10,85,866 cr ÷ (1,353.2 cr shares × ₹1,190.0) = 0.674. Both are on one basis. The spec's 0.0786 and 1.0445 were FY2024 pre-bonus figures against a bonus-adjusted price.

**D2.** The API side is R4. The pipeline side: re-scoring a date now replaces its rows, and the score step reports stale prices.

**D3.** The 0.23 coverage came from two things:
- **Older model.** The 31 Jul and 14 Aug runs used model `…a8756fe2`, which has a 0.2 floor. The current model `…4b05e87d` uses 0.5, which is the code default and what every run since 15 Aug used.
- **Fundamentals not in scope.** FY2025/26 annual rows were extracted after those runs.

Re-scored on 24 Oct with the fixes, average coverage is 0.872 and the lowest is 0.60 (see "Real data").

---

## Notes for the UI, per requirement

**R1.** `GET /v1/instruments/{isin}/explanation?as_of=YYYY-MM-DD` is built from that date's signal and factors, not the latest signal's. A date with no row is a 404 naming the date, with no nearest-date fallback. `stale`, `stored_version` and `current_version` work as before.

**R2.** `GET /v1/instruments/{isin}/score-status` returns 200 for any known ISIN.
- **`reason`** is one of `scored`, `not_in_latest_run`, `below_coverage_floor`, `missing_fundamentals`, `sector_not_supported`, `never_scored`.
  - **`sector_not_supported`** covers banks and life insurers. It is read from the exchange's results-format name in the filing's file name (`BANKING`, `LI`); every annual filing in those formats is refused for missing revenue or equity tags.
  - **`missing_fundamentals`** means no annual results were stored by that run, for any other reason.
- **`scored`** is true when the newest run that measured the company stored a signal for it. A company scored on 14 Aug and measured but not scored on 24 Oct is `scored: false`, with `latest_as_of = 2026-08-14` (what `/latest` returns) and `measured_as_of = 2026-10-24` (the run the reason describes). NTPC, TATAPOWER and NESTLEIND are in this state on the real data.
- **Added fields:** `measured_as_of`, `factors_measured` and `factors_total`.
- **`coverage`** is replayed from the stored z-scores and equals what that run compared with the floor.
- **`detail`** is one sentence, ready to display.

**R3.** `/v1/model` adds `version`, `coverage_floor`, `families`, `factors` and `red_flags`, all read from the Python definitions. A test fails if a factor is added without a label and a unit. Units:
- **`percent`, `pp`, `multiple`, `ratio_vs_trend`, `score`** as specified.
- **`ratio` (added):** `peg_inverse` is earnings yield × profit growth. RELIANCE's is 0.0033; as a two-decimal multiple it would show 0.00×.
- **`relative_strength_6m` is `pp`, not `percent`** as the UI's frontend map had it. It is a 6-month return minus the universe's median return.

`unit` is also on `FactorOut` and `DriverOut`; it is null for a stored factor the current model no longer defines.

**R4.** `GET /v1/signals?latest=per_instrument` gives each instrument's newest row, ranked together, each with its own `as_of`. It combines with `signal` and `sector`; with `as_of` it is a 422. The default list is unchanged.
- **`/v1/health.latest_complete_as_of`:** the newest date on which at least 50% of instruments active that day got a signal. An instrument with no listing date counts as listed, which is all 100 today.
- **`/v1/model.scored_date_counts`:** `[{as_of, scored, instruments}]`, newest first.

**R5.** `previous: {as_of, composite_score, signal, model_version} | null` is on every `SignalOut`: `/signals` (both modes), `/signals/red-flags`, `/latest` and `/history`. It is read from the whole table, not the page, so a one-row history still has its previous. Checked on the real data: ABB on 16 Aug → 15 Aug at 50.0; RELIANCE on 16 Aug → 14 Aug.

**R6.** `GET /v1/instruments/{isin}/shareholding?quarters=12`, newest first.
- **Percentages** are 0–100, not fractions.
- **`disclosed_date_source`** is `NSE` or `ASSUMED_LODR_DEADLINE`. It is ASSUMED for every stored row today, meaning the 21-day deadline stands in for the disclosure date.
- **`promoter_pledged_pct`** is always null: the NSE feed carries no pledge figure. Null means unknown, not zero.
- **`fii_pct` and `dii_pct`** are null in the real data: the feed did not break them out.

**R7.** `GET /v1/instruments/{isin}/key-metrics`, taken at the company's newest close (`as_of`) and read only from filings made by then.
- **Same basis as the factors.** It uses their own share count and price basis, so `pe_annual` is exactly 1/`earnings_yield` and `pb` exactly 1/`book_to_price`. This is checked in the tests and against the dry-run scores for RELIANCE, BAJFINANCE and TVSMOTOR.
- **Both P/Es.** `pe_ttm` uses the four newest quarters, and is null unless they are consecutive. `pe_annual` is the factor's.
- **Knowledge dates:** `shares_filing_date`, `ttm_through`, `ttm_filing_date`, `annual_filing_date`.
- **Dividends** come from NSE corporate actions. `dividends_ttm` is null when actions were never fetched for the company, and 0 when they were fetched and nothing was paid.
- **Example (dry run, a day earlier):** RELIANCE on 22 Oct: ₹1,184.5, 1,353.3 cr shares (stated), market cap ₹16.03 lakh cr, P/E 21.5 TTM / 19.8 FY26, P/B 1.48, dividend yield 0.51%.

**R8.** `GET /v1/instruments/{isin}/corporate-actions`, newest first: `ex_date`, `action_type`, `ratio` (split/bonus: shares after per share before), `amount` (dividend ₹/share), `record_date`, `details`, `source`. `eps_adjusted` is on every annual and quarterly row: EPS restated across the splits and bonuses after the filing. RELIANCE's Sep '24 quarter reads 24.48 → 12.24, beside Dec '24's 13.70.
- **Bonuses of preference shares (NCRPS) or debentures** are not share changes: TVSMOTOR's 2025 "Bonus Ncrps 4:1" and Britannia's debenture bonus.

**R9.** `QuarterOut` adds `basis`, `total_income`, `other_income`, `total_expenses`, `depreciation`, `interest_expense`, `profit_before_tax`, `tax_expense` and a derived `ebitda`. EBITDA is PBT + finance costs + depreciation, the value factor's formula. It is null for banks and insurers, whose finance cost is their main operating cost. Values are ₹ crore. They are null on the few rows from NSE's results comparison, and for a line a filing doesn't tag (a bank's quarter tags no PBT or depreciation).

**R10.** `source_url` on every annual and quarterly row is the document the figures were read from: the results filing's XBRL instance (XML) on nsearchives.nseindia.com, or the annual-report PDF for the LLM path.
- **Why not via `filings`:** the spec suggested publishing that table, but it holds only three annual-report PDFs. So the link is stored on the row, and the bookkeeping table stays unpublished.
- **Gaps:** the instance is the filing the numbers came from, but it is XML, not the PDF announcement a person would open. Rows from NSE's results comparison have no link.

**R11.** `/history` rows add `families: [{family, percentile, contribution}]`, identical to that date's `/explanation`. A row stored under another model is re-derived with today's weights, as the explanation's `stale` flag says.

**R12.** `/news` items add three fields:
- **`relevance`:** `primary` (named in the headline of an article about it), `mention` (named only in the body, or one company in a roundup) or null (not recorded).
- **`sentiment_status`:** `scored`, `pending` (will be scored on the next run), or `not_scored` (the company match is below the attribution threshold, so it never will be).
- **`counts_toward_sentiment`:** whether the 30-day factor reads the article.

`?relevant_only=true` keeps the primary ones. Two differences from the spec:
- **No `failed` status.** The pipeline records no per-article scoring failure; a failed run leaves its articles `pending`.
- **RELIANCE's 7 newest unlabelled items are `not_scored`**, not pending: they are roundups and body mentions. The −0.96 "stocks to buy" piece keeps its label but `counts_toward_sentiment: false`.

**A fix that changes a number on the page:** the explanation's news mix now applies the threshold the factor reads through. It used to count that roundup against RELIANCE.

**R13.** `InstrumentOut.industry` is returned, but for this universe it repeats the sector. `bse_code` is filled from the company's results filings (the BSE code every filing names itself by) during the quarterly re-read.
- **`listing_date` stays null** (deferred): no source for it is ingested. NSE's equity master list and its quote API both carry it; picking one is the owner's call.
- **`description`, `website`, `logo_url`** are deferred, as the spec says.

**R14.** `/v1/health` adds `latest_price_date`, `latest_filing_date` (the newer of quarterly and annual) and `latest_news_date` (articles attributed to a company).

**R15.** `/instruments/search` results add `as_of`, `composite_score` and `signal` from each company's newest signal. They are null when it has none.

**R16.** `FactorOut` (in `/latest`) and `DriverOut` (in `/explanation`) add `sector_median`, `sector_n` and `sector_percentile`. The percentile ranks the raw value ascending, so for a lower-is-better metric 100 is the worst.
- **Added `zscore_basis`:** `sector`, or `universe` when fewer than five sector companies had a value. In that case the z-score was taken against the whole universe and the sector figures are context only. Without this, a "vs sector" tooltip would mislabel those z-scores.

**R17.** `FinancialsOut.unit = "INR crore"` and `per_share_unit = "INR"`. `LineItemOut.per_share` says which applies.

---

## Real data

**Re-scored and re-published on 24 Oct 2026**, with the owner's go-ahead. The steps:
- **Backup:** `data/stockanalysis.duckdb.bak-2026-10-24`, byte-identical to the database before the run.
- **Update:** `update --steps prices,corporate-actions,quarterly,xbrl,score --reread-filings --as-of 2026-10-24`, exit 0.
- **Publish:** `stockanalysis publish` put release `20261024_142712` live (157,892 rows across 10 tables, `corporate_actions` included). The previous release, `20261022_222244`, is kept for `stockanalysis publish --rollback`.

**What changed in the data:**
- **The stale 24 Oct run is replaced.** 85 companies are scored on 24 Oct, all under model `…4b05e87d`: 8 BUY, 25 HOLD, 52 SELL. Average coverage is 0.872, the lowest 0.60. `latest_complete_as_of` is 2026-10-24.
- **The other 15 each have a stated reason** in `score-status`:
  - 11 are `sector_not_supported`: the banks, plus HDFCLIFE and SBILIFE.
  - 4 are `missing_fundamentals`: ENRIN, NESTLEIND, NTPC and TATAPOWER (see Follow-ups).
- **Of the spec's 22 unscored companies,** every company that is neither a bank nor an insurer is now scored except ENRIN (10 of 11). Examples are BAJAJHLDNG at 0.87, TATACAP at 0.62 and TMCV at 0.60.
- **Corporate actions:** 1,383 from NSE. Of them, 45 bonuses across 29 companies, with TVSMOTOR's NCRPS bonus correctly excluded, and 19 splits.
- **Filled by the re-read:**
  - `bse_code` for all 100 companies;
  - quarterly P&L lines on 1,080 of 1,088 rows and links on 1,084;
  - annual links on 297 of 299 rows.

**The spec's "done when" examples, checked through the API on the real data:**
- **R1:** RELIANCE on 31 Jul reads 32.5 under `…a8756fe2`, with `stale: true`. A date with no row is a 404.
- **R4:** `latest=per_instrument` gives 88 rows: 85 from 24 Oct and 3 from 14 Aug.
- **R5:** on 16 Aug, ABB's previous is 15 Aug at 50.0, and RELIANCE's previous is 14 Aug.
- **R7:** at the 23 Oct close (RELIANCE ₹1,190), `pe_annual`/`pb` equal 1/EY and 1/BP to three decimals:
  - RELIANCE 19.936 / 1.483
  - TCS 18.602 / 8.443
  - BAJFINANCE 30.358 / 4.942
- **R8:** RELIANCE Sep '24 EPS 24.48 → 12.24 adjusted, next to Dec '24's 13.70.
- **R12:** RELIANCE's newest items are `mention`/`not_scored`.
- **R13:** RELIANCE's `bse_code` is 500325.

**Parity on the real data:** 210 `/v1` URLs return byte-identical JSON from DuckDB and from the published Postgres copy. They cover every route for eight companies, plus `score-status` for all 100.

**Operational note:** the re-read took 33 minutes, against 2.5 for the dry run. A worktree's `data/cache` starts empty, so all 1,093 quarterly and 553 annual instances were downloaded at the 1-request-per-second limit. Run from the repo root, where the cache is warm, it is a few minutes. The 181 parse failures (pre-2022 instances that aren't XBRL) and 40 validation refusals are the same as in the dry run.

---

## Follow-ups (not done here)

- **Annual XBRL refusals that leave real companies unscored:**
  - NESTLEIND: its PAT is read as 0.0 and fails the profit identity, which looks like a parser problem.
  - NTPC and TATAPOWER: the PBT → PAT identity fails once share of associates is added.
  - ENRIN: `ocf` is untagged, and FY2025 is a half-year filing.

  `score-status` reports them as `missing_fundamentals`.
- **News resolver:** "Reliance, Rolls-Royce join hands…" names Reliance in the headline but was matched in the body at 0.665, so it is a `mention` and doesn't count.
- **`tests/test_factor_model.py::test_injecting_future_fundamentals_does_not_move_todays_score` may be vacuous.** The factor instances read the module-level `PANEL_CACHE`, not the cache passed to `CompositeModel`, so both scorings may share one panel.
- **The FinBERT agreement test** fails without `torch`; its skip condition should also require torch.
- **The `readiness` datasets don't list corporate actions.**
- **A one-company job still re-scores and persists the whole universe.** It now warns about stale prices, but a job could persist only its own company's signal.
- **`listing_date` source** (R13), and the profile fields `description`, `website` and `logo_url`.
