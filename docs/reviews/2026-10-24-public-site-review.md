# Public site review: `claude/ui-design-c663e2` (web/, 8d31889..c308744)

**Date:** 2026-10-24 · **Reviewer:** independent session (not the author) · **Scope:** `git diff main...HEAD`: the Next.js app in `web/`, the design doc, the approved mock, the plan, the API-requirements doc and `.claude/launch.json`.

**Counts:** 3 blockers · 11 should fix · 16 minor · 5 compliance flags for the owner's lawyer (these are not code defects)

---

## How this was checked

- `npm ci`, then `npm test` (140/140), `npm run typecheck`, `npm run lint`, and `npm run build` with `SA_API_URL` pointed at a dead port. All pass.
- Playwright smoke suite: 22/22 pass on the installed Chrome. I ran it against a production server (`next start -p 3001`) through a throwaway config, because `:3000` was another worktree's dev server (see M7).
- Live pages against the local API on :8000, at 1440×900 and at 375×812, for RELIANCE, ABB, HDFCBANK, CHOLAFIN, ADANIENSOL, GAIL and ULTRACEMCO, plus `/`, `/signals`, `/red-flags`, `/learn`, `/disclaimer` and `/stocks/NOPE`.
- Two more throwaway servers:
  - one with the API unreachable (`SA_API_URL=http://127.0.0.1:9`)
  - one with a trailing slash (`SA_API_URL=http://localhost:8000/`)
- Every number below was compared with the raw API response, and every catalogue with the Python source.
- A contrast sweep ran in the browser over every text node on the RELIANCE page.

**What is right.** Most of the work is careful, and these all hold up:

- **Catalogues match the Python source.**
  - `FAMILY_MEANING` in `families.ts` is identical to `explain.py`.
  - The six `RED_FLAGS` descriptions and `reachable` values match `redflags.py::DEFINITIONS` once the thresholds are formatted.
  - `FACTOR_UNITS` covers all 20 `FACTOR_LABELS`. I spot-checked accruals, margin_trend, interest_coverage, price_to_200dma, roce, debt_to_equity and peg_inverse in `factors/*.py`.
  - Thresholds and weights are read live from `/v1/model`, not hard-coded.
- **Prices are right.**
  - RELIANCE 1Y figures match the API: −₹63.80 and −4.64%; 52-week closes ₹1,592.30 and ₹1,258.80.
  - `close` is already adjusted for splits and bonuses (no jump at the Oct 2024 1:1 bonus).
  - Indian digit grouping, ₹ and the true minus sign are consistent.
  - Dates are parsed from their parts, so I found no IST/UTC bug.
- **Awkward states mostly behave as designed.**
  - Unscored companies never show a 0.
  - Unchecked red flags are never shown as clear.
  - Unscored news is never counted as neutral.
  - CHOLAFIN's override banner and stale-model notice render.
  - The model-change warning appears in the compare view.
- **Security basics are sound.**
  - The proxy can't change host or escape `/v1`. I tried `..`, `%2e%2e`, `..%2F`, `%5C..` and `@host`; all 404 at the API.
  - The proxy forwards no request headers and returns only `content-type` and `cache-control`.
  - React 19 blocks `javascript:` hrefs on server and client, and news links carry `rel="noopener noreferrer"`.
- **Outages are handled.** With the API down, data pages return 500 with "Data is temporarily unavailable", and `/disclaimer` still renders.
- **Phone layout works.** At 375px there is no horizontal scroll on the stock page, and the phone order matches the spec.

---

## Blockers

### B1. "Coverage" is the share of model *weight* with data, but every page calls it a share of *factors*

- **Where:**
  - `web/src/components/stock/WhyCard.tsx:49` ("90% of factors measured", "Based on 23% of factors…")
  - `web/src/components/stock/ScoreCard.tsx:56-58` ("only N% of factors measured")
  - `web/src/components/charts/ScoreHistoryChart.tsx:115,134` ("N% of factors")
  - `web/src/components/learn/LearnView.tsx:80` ("the share of the model's factors that could be measured")
  - The design doc (§5.1, §6.2 #5) uses the same wording.
- **What a visitor sees:** open `/stocks/RELIANCE`. The Why card says **"90% of factors measured"**.
  - `/v1/.../explanation` reports `coverage: 0.9`, and its own `families[].factors_measured` add up to **19 of 20 factors (95%)**.
  - ABB reads "73%" against 15 of 20 (75%).
  - CHOLAFIN reads "23%" against 5 of 20 (25%).
- **Why it matters:** `composite.py::_aggregate_families` defines coverage as `Σ family_weight × (available intra-family weight ÷ total)`. That is a share of the model's weight, not a count of factors. The site states a figure as something it isn't, on every scored page. Fundamentals-literate visitors (the target audience) can count the factors in the breakdown and will find a mismatch. The tests pin the wrong copy (`stock-top.test.tsx:25,79,104`).
- **Fix:** relabel it as "Data behind 90% of the model's weight", and in the Learn copy, "the share of the model's weight backed by data". Optionally, also show the factor count the API already returns (Σ`factors_measured` / Σ`factors_total`, "19 of 20 factors"). Update the tests and the design doc to match.

### B2. The breakdown's weight column and formula don't match how contribution is calculated

- **Where:** `web/src/components/stock/BreakdownCard.tsx:42` (Wt column) and `:47` (footnote "Contribution = weight × family z-score").
- **What a visitor sees:** open `/stocks/CHOLAFIN` → Score breakdown.
  - Value, Quality and Sentiment are "n/m" but still show weights of **25% / 30% / 10%**.
  - Growth shows **20%**, and the footnote says contribution = weight × z.
  - From the API, growth z = 0.369, so 0.20 × 0.369 = **0.074**. The API's `contribution` is **0.211**, because `explain.py::_family_rows` divides by the measured families' total weight (0.35).
  - RELIANCE is off by 1/0.9, because sentiment is unmeasured. That is true for every scored company today.
- **Why it matters:** the breakdown is the "why" centrepiece. The page states a formula that is false for 100% of scored companies, and the weight column shows weight a family isn't getting. The "re-weighted" sentence admits it happens without showing the weights that were used.
- **Fix:** show the effective weight (`weight ÷ Σ weight of measured families`). For example: CHOLAFIN Growth "57% (of 20%)", Momentum "43% (of 15%)", unmeasured families "—". Change the footnote to "Contribution = family z × weight, re-weighted over the measured families". Also consider printing the contribution number (it is only a bar today, with the value in `title`).

### B3. Every 404 from the API is read as "no data", so a wrong address or a missing route makes real data disappear without warning

- **Where:**
  - `web/src/lib/api/client.ts:30` (`if (res.status === 404) return null;`)
  - `client.ts:35` (`apiBase` doesn't normalise the URL)
  - `web/src/app/red-flags/page.tsx:12-16` (null → `[]`)
  - `web/src/components/shell/SearchBox.tsx:64` (null → `[]`)
- **What a visitor sees** (reproduced with `SA_API_URL=http://localhost:8000/`, one trailing slash, so every call goes to `//v1/...`, which the API answers 404 `{"detail":"Not Found"}`):
  - `/stocks/RELIANCE` → HTTP **404**, "**No NIFTY100 company with symbol RELIANCE**".
  - `/red-flags` → HTTP **200**, "**No company has a triggered red flag on the latest date.**"
  - Search for "rel" → "No NIFTY100 company matches 'rel'".
  - The same happens for any route the backend session renames or hasn't deployed yet. The site and the API ship separately, and `/v1` is changing right now (R1–R17).
- **Why it matters:** these are the spec's own anti-goals. Missing data looks like a real answer ("doesn't exist", "no red flags") instead of an outage, the status codes are wrong, and nothing alerts anyone.
- **Fix:**
  - Treat a 404 as "absent" only when the body is a domain answer. The API's domain 404s say `"No instrument for symbol 'NOPE'"` and `"No signal stored for '…'"`, while an unknown route says `"Not Found"`. Throw `ApiUnavailableError` on `"Not Found"`.
  - Strip trailing slashes in `apiBase()`.
  - Make `/red-flags` throw when `/signals/red-flags` or `/health` returns null, as `/learn` already does.
  - Show "Search is unavailable" when search returns null.

---

## Should fix

### S1. Partial runs and model changes are presented as comparable scores

The site knows a date is partial (`partialNotice`) but warns only on `/` and `/signals`. Live API check:

| Date | Companies scored | Model |
|---|---|---|
| 16 Aug | 4 | ABB 93.3, ADANIENSOL 32.6, ADANIGREEN 31.7, RELIANCE 28.4 |
| 15 Aug | 1 | ABB alone, exactly 50.0 (a single standardised value) |
| 14 Aug | 77 | `a8756fe2` |

So the 16 Aug scores are ranks among four companies. The places this leaks:

- **Stock page, RELIANCE and ABB** (`ScoreCard.tsx:14,44-47`):
  - The cards read "SELL 28/100" and "BUY 93/100".
  - The tooltip says "relative rank … among scored NIFTY100 stocks".
  - Nothing says the ranking was against three other companies.
- **Signals list** (`lib/signals/list.ts:8-15`, `SignalsView.tsx:34-46`):
  - ABB shows **"+43.3"** in green under "Change since 15 Aug 2026", with no dagger because the model is the same.
  - The tests pin it (`signals.test.ts:26,69`).
- **Trend card** (`TrendCard.tsx:13-29`):
  - RELIANCE reads "−4.1 points since first scored, 31 Jul 2026", and ABB reads "+17.1".
  - Both cross the a8756fe2 → 4b05e87d model change and a coverage jump from 23% to 90%.
  - The only hint is a small "model updated" label inside the sparkline. This is design anti-goal 4.
- **Compare view** (`ScoreHistorySection.tsx:91`): it warns only when the model differs. ABB 15 Aug → 16 Aug shows "+43.3 points · the signal changed" with no warning.
- **Home** (`HomeView.tsx:73-90`): ABB is the top "Highest scores" BUY.

**Fix:**
- Fetch the row count per scored date once. It is 4 small calls, or use R4 when it lands.
- Mark snapshots from runs below 50% of the universe as "partial run (4 companies)" in the history table, chart tooltip and compare view, and in the score card's as-of line.
- Treat a partial-run or cross-model delta like a model change: dagger plus a note, not a bare green "+43.3".
- On the trend card, say "across a model change" when `modelChangeIndexes` is non-empty.

This is a launch blocker on today's data even though the cause is in the pipeline (design §10.2).

### S2. Unscored companies' peers and the red-flags page use the partial date

- **Where:**
  - `web/src/lib/stock/load.ts:42` (`model.scored_dates[0]`)
  - `web/src/components/stock/PeersSection.tsx:16-21`
  - `web/src/app/red-flags/page.tsx:12` (no `as_of`, so the API's latest)
- **What a visitor sees:**
  - `/stocks/HDFCBANK` → "No other Financial Services company was scored on 16 Aug 2026". Seven were scored on 14 Aug, and "other" wrongly implies HDFCBANK was scored.
  - `/stocks/ULTRACEMCO` → "No other Construction Materials company…". AMBUJACEM, GRASIM and SHREECEM were scored on 14 Aug.
  - `/stocks/GAIL` → a peer table of RELIANCE alone, from the four-company run.
  - `/red-flags` → "Flagged on 16 Aug 2026" lists only ADANIENSOL, with no partial-run notice. CHOLAFIN's promoter_selling flag on 14 Aug isn't shown.
- **Fix:**
  - For unscored companies, use the latest date with at least 50% of the universe. `findFuller` in `lib/signals/load.ts` already does this.
  - Reword to "No {sector} company was scored on {date}" when the company itself wasn't scored.
  - Give `/red-flags` the same notice and fuller-date link as `/signals`.

### S3. Financials use one consolidated/standalone label for columns that are on different bases

- **Where:** `web/src/components/stock/FinancialsSection.tsx:20-23`, `:53`.
- **What a visitor sees:** open `/stocks/ABB` → Financials. The header says "**consolidated**".
  - Per the API, every ABB quarter has `is_consolidated: false`.
  - The annual columns are FY25 CONSOLIDATED, FY24 and FY23 STANDALONE.
- **Why it matters:** the label is wrong for most columns, and the annual row compares consolidated with standalone figures without saying so.
- **Fix:** take the basis per column: put "C"/"S" under each column header or in the "Filed" row, and drop the single section-wide basis. Add a note when the bases are mixed.

### S4. Dates and the disclaimer fail contrast

- **Where:** `web/src/app/globals.css:12` (`--ink3: #8C8C9C`), used by `.asof` (`globals.css:54`) and `.disc` (`shell.module.css:54`).
- **Measured on `/stocks/RELIANCE`:**
  - "Scored 16 Aug 2026 · model 4b05e87d": 3.31:1
  - The footer disclaimer: **3.04:1**
  - The "?" unchecked-flag chips: 2.96:1
  - "n/m", "SELL < 45": 3.31:1
  - WCAG AA needs 4.5:1 for text this size.
- **Why it matters:** as-of dates and the disclaimer are the two things the spec makes non-negotiable, and they are the least legible text on the page.
- **Fix:** darken `--ink3` to about `#6B6B7E` (≈5.2:1 on white, ≈4.8:1 on `#F5F5F8`), or give `.asof` and `.disc` `--ink2`. Re-run the sweep.

### S5. Overturn the round-down ruling

- **Where:** `web/src/lib/format.ts:106-113` (`scoreText`).
- **The ruling:** 74.6 reads 74, so it sits correctly beside HOLD. That part is right.
- **What it costs:**
  - Every whole-number score reads up to a point low. The API's own headline for ADANIENSOL says "overriding a factor score of **33**/100", while the site shows **32**.
  - The project's Streamlit dashboard (`dashboard.py:79`, `.1f`) and the LLM narrative prompt (`narrative.py:206`, `.1f`) show 32.6. If a narrative is ever shown in the Why card (`WhyCard.tsx:53`), its number disagrees with the score card above it.
  - One-decimal values are also floored (`scoreText(x, 1)`), but deltas are rounded half-up from raw values. A visible pair like 32.5 → 28.4 can sit beside "−4.2".
- **Fix (pick one):**
  - **Option A (recommended):** show scores to one decimal everywhere, as the dashboard and narrative already do. "74.6 / 100" beside "BUY ≥ 75" is unambiguous for this audience.
  - **Option B:** round normally, and floor only when rounding would cross a threshold the raw score is below. Use ordinary rounding for one-decimal values.

  Either way, compute displayed deltas from the displayed values, or show deltas and scores to the same precision.

### S6. The disclaimer is far from the verdict, and the signal's meaning is hidden in tooltips

- **Where:**
  - `AppShell.tsx:70` (disclaimer only at the page foot)
  - `SignalChip.tsx:11` (meaning only in `title`)
  - `InfoTip.tsx` (hover or keyboard focus only)
  - `HomeView.tsx:47` ("Today's picture")
- **What a visitor sees:**
  - On a phone, the "SELL" chip is at y≈350px and the disclaimer at y≈5,500px, about **6.8 screens** down.
  - "Our model ranks it in the bottom band…" and "Not a valuation or a price target" exist only as hover text. They aren't visible on touch, and the ⓘ is 15×15px.
  - Home calls 16 Aug data (69 days old) "**Today's** picture".
- **Why it matters:** this is anti-goal 1 ("reads as personal advice"), and it is what a regulator or lawyer will look at first, whatever label is chosen. See the compliance section.
- **Fix:**
  - Put a visible one-line caption beside every score and chip cluster: on the score card, home lists and the signals header. For example: "Model rank on 16 Aug 2026, not advice · How to read this".
  - Make the ⓘ a button that toggles visible text, with a target of at least 24px.
  - Rename the home card "Latest scores ({date})".

### S7. Limit the proxy to what the browser needs, and add a timeout

- **Where:** `web/src/app/api/v1/[...path]/route.ts:6-21`.
- **The problem:**
  - Only `SearchBox` uses the browser path (`browserGet` has one caller), but the route forwards every `GET /v1/*` with `cache: "no-store"`.
  - It has no upstream timeout and no rate limit. A slow API ties up the site's server connections, and the site becomes a public, CORS-free mirror of the whole API.
- **Fix:**
  - Allowlist `instruments/search`. Add others only when a browser caller exists.
  - Add `AbortSignal.any([request.signal, AbortSignal.timeout(5000)])`.
  - Normalise `SA_API_URL` (see B3).

### S8. Scores from an earlier model that the current model wouldn't publish

- **Where:** `ScoreCard.tsx:44-47`, `BreakdownCard.tsx:24-29`, `LearnView.tsx:80`.
- **What a visitor sees:** `/stocks/CHOLAFIN` → "SELL 87/100, Based on 23% of factors, treat with caution".
  - The current model (`4b05e87d`, `ScoringConfig.min_coverage = 0.5`) would not score a company at 23% coverage.
  - 76 of the 77 latest scores for most companies are at 23%, all from `a8756fe2`.
  - The stale-model note sits in the breakdown, not on the score.
- **Why it matters:** it reads as "a weak score", when the model's own rule says "no score". That is the spec's "missing data looks like good data" concern.
- **Fix:** this is an owner decision. Either show these as "Scored under an earlier model (23% coverage); the current model requires 50%" on the score card itself, or treat them as not scored until a full re-run. Read the floor from `/v1/model` when R3 lands, rather than `LOW_COVERAGE`.

### S9. "52-wk high/low" are closing prices

- **Where:** `web/src/lib/prices.ts:41-46`, `web/src/components/stock/PriceCard.tsx:55-60`.
- **What a visitor sees:** RELIANCE shows **₹1,592.30 / ₹1,258.80**, which are the highest and lowest closes. The API's intraday high and low for the same year are ₹1,611.80 and ₹1,249.80, which is what NSE and Tickertape print as the 52-week high and low.
- **Fix:** keep `high`/`low` in the compact series and use them, or label the figures "52-wk closing high/low".

### S10. The page doesn't say when the LLM narrative is machine-written

- **Where:** `web/src/components/stock/WhyCard.tsx:53`.
- **The problem:** `narrative` comes from `serve/narrative.py` (Claude). It is rendered as plain prose under the verdict, with no label. It is null in today's data, so the problem is latent.
- **Fix:** label it ("Written by an AI model from the figures above; it cannot change the score") and keep its numbers out of headline positions. This also bears on the SEBI questions below.

### S11. The tests pin the behaviours above

- **Where:** `stock-top.test.tsx:25,79,104`, `stock-top.test.tsx:48-49`, `signals.test.ts:26,69`.
- **The problem:** the tests assert "90% of factors measured", ABB +43.3, and "−4.1 points since first scored" with no model caveat. They test behaviour (good), but they lock in the wrong behaviour.
- **Fix:** update them with B1, S1 and S5. Add cases for:
  - a route-level 404 (B3)
  - mixed financial bases (S3)
  - an unscored company's peers on a partial date (S2)

---

## Minor

| # | Where | Finding | Suggested fix |
|---|---|---|---|
| M1 | `web/src/app/stocks/[symbol]/page.tsx:19-31` | **Known limitation, root cause found.** `/stocks/NOPE` returns Next's *error shell* (`<html id="__next_error__">`, empty body, title "StockAnalysis"). `/nonexistent-page` server-renders its 404 normally, so the problem is specific to `notFound()` here, not to not-found pages in general. | Untested suggestions: call `notFound()` from `generateMetadata` as well (it currently returns a title), or resolve the instrument in a `[symbol]/layout.tsx`. Check with curl that the server HTML contains the h1. |
| M2 | `web/next.config.ts:5` | No security headers. `X-Powered-By: Next.js` is sent; there is no CSP, `frame-ancestors`, `X-Content-Type-Options` or `Referrer-Policy`. | Add `poweredByHeader: false` and a `headers()` block before launch. |
| M3 | `web/src/components/stock/NewsCard.tsx:54-57` | News `url` is rendered as given. React 19 blocks `javascript:`, but other schemes pass. | Render a link only for `http:`/`https:` URLs (an allowlist). |
| M4 | `web/src/components/shell/AppShell.tsx:33-44` | No skip link. There are about a dozen tab stops (sidebar, ⓘ, breadcrumb, search) before the stock header. | Add "Skip to content" targeting `<main>`. |
| M5 | `RedFlagsCard.tsx:25`, `PeersSection.tsx:55`, `SignalsView.tsx:171`, `SegmentMeter.tsx:32` | Some state is shown only by symbol, colour or `title`: flag-chip state (✓/✗/? plus `title`), amber low coverage with no text, and meter labels set `aria-hidden`. The spec says "never colour alone". | Add visually hidden state text, for example "(low coverage)". |
| M6 | `web/tests/fixtures/*/prices.json` | 1.4 MB of pretty-printed bars (5 × ~1,490 bars × 7 fields). Only RELIANCE's series is asserted against. | Store compact `[date, close, volume]` JSON (RELIANCE: 310 KB → 46 KB), or trim the other four to about 260 bars. |
| M7 | `web/playwright.config.ts:13-19` | `baseURL` is hard-coded to :3000 with `reuseExistingServer: true`. With another worktree's dev server on :3000 (true today), `npm run e2e` tests that worktree's code and passes. | Read `PLAYWRIGHT_BASE_URL`/`PORT` from the environment, and fail if `/api/v1/health` isn't served by this checkout. |
| M8 | `web/.env.example:2`; design doc §7 line 232; plan Task 1 step 2 | They still describe a `next.config` rewrite. | Point them to the proxy route. |
| M9 | `page.tsx` files (`connection()`), all routes | Every page is dynamic with `Cache-Control: private, no-store`. Each stock view makes 7–8 API calls, cached 300s per server instance, and a CDN can cache nothing. Fine for v1 traffic. | Before launch, consider route-level `revalidate` or ISR for `/stocks/[symbol]` once the 404 path (M1) is solved. The stock page is 140 KB HTML (32 KB gzip, ~1,490 price pairs inline) plus ~198 KB gzip JS, which is acceptable. |
| M10 | `web/tests/unit/config.test.ts:25` | The catalogue tests compare against a hard-coded list, not the Python source. They match today, but nothing catches drift. | Until R3 lands, add a pytest that dumps `FACTOR_LABELS`, `FAMILY_MEANING` and `DEFINITIONS` to JSON, and a vitest that compares against it. |
| M11 | `web/src/lib/signals/load.ts:107-110`, `HomeView.tsx:85-89` | With 4 scored, "Highest scores" lists ADANIENSOL at 32 SELL. The home "Red flags" card has no date while its neighbours do. | Hide the top/bottom lists, or show the fuller date, when the run is partial. Add the date. |
| M12 | `web/src/lib/stock/state.ts:55-59` | ADANIENSOL (auditor flag triggered) reads "13 pts to HOLD", but no score can lift it out of SELL while the flag stands. | When `red_flags` is non-empty, say "Red flag holds it at SELL". |
| M13 | `ScoreCard.tsx:14`, `LearnView.tsx:25-27`, `SignalsView.tsx:210` | The copy says the score is "built from factor percentiles" and is a "percentile rank". Per `composite.py:227` it is Φ(standardised composite z) × 100, and the family z is a *weighted* mean, re-standardised. | Reword ("a normal-curve percentile of the weighted family z-scores"). |
| M14 | `ScoreHistorySection.tsx:51` | **Overturn ruling.** The compare picker defaults to the *earliest* snapshot, so RELIANCE and ABB open on a cross-model comparison with a warning. | Default to the previous snapshot. |
| M15 | `SearchBox.tsx:104` | Typing a symbol and pressing Enter within 150 ms does nothing. | On Enter with no results yet, go to `/stocks/{QUERY}`. |
| M16 | Error pages (`app/error.tsx`) | The outage view has no `<title>` and no shell or nav (author's deferred item, related). | Give it the app shell and a title. |

### The author's rulings and deferred minors

Rulings I would overturn:
- **Scores round down:** overturn (S5).
- **Compare picker defaults to the earliest snapshot:** overturn (M14).
- **52-week high and low use the last year:** keep it, but label them as closes (S9).
- **Removed `cacheComponents` to get real 404s:** keep for now. The 404 body is still empty (M1) and pages can't be cached (M9), so revisit both together.

Every other ruling I agree with. That includes per-page app shells, `retry`, newest-first financials, and signal words only in config.

Deferred minors from the author's report (I agree with deferring all six):
- "n/m" vs "Not scored", and the two views that hard-code "Not scored": keep deferred.
- "1 pts to BUY": keep deferred; it's trivial.
- Before the first scoring run, home and signals say "temporarily unavailable" rather than "no scores yet": keep deferred.
- The outage message shows for any rendering error: keep deferred, but note that production error messages are hidden, so it can only be fixed by not throwing for non-outage cases.
- `/stocks/RELIANCE%20` returns 404: keep deferred.
- The volume and its date can disagree if the last bar has no close: keep deferred.

---

## Compliance flags for the owner's lawyer (flagged, not decided)

1. **BUY / HOLD / SELL on named NIFTY100 stocks, published to the public.** The owner already knows this generally needs SEBI Research Analyst registration. Under the Dec 2024 amendments, a research analyst is someone who, *for consideration*, provides research services, and "research services" and "trading calls" are now defined terms. So whether a free site (or one with ads or a later paid tier) is caught is a legal question. SEBI's July 2025 FAQ excludes general market commentary and broad indices from "research report". Per-stock BUY/SELL is not that.
2. **How the page reads, whatever the legal answer** (fix regardless, S6):
   - The verdict chip is a bare "BUY"/"SELL" with its model-rank meaning hidden in a tooltip.
   - The only disclaimer is at the page foot (6.8 phone screens below the verdict) at 3:1 contrast.
   - The home page says "Today's picture".
   - Copy never addresses the visitor ("you should"), which is good.
3. **The disclaimer asserts a legal conclusion.** `DisclaimerView.tsx:35` says "Nothing on this site is investment advice or research under any regulation". Let the lawyer decide whether to assert that. If registration turns out to be needed, the sentence is false.
4. **AI-written text.** Narratives come from an LLM (S10). The 2024 RA amendments added obligations on research analysts' use of AI tools. Ask whether labelling is required, not just good practice.
5. **Third-party recommendations in the news list.** RELIANCE's scored news includes a "Stocks to buy: … recommends Bank of Maharashtra…" headline scored −0.96 against RELIANCE, shown under our SELL. This is data quality (design §10.4), but it puts someone else's buy call on our stock page.

---

## Verdict

**Fit to merge once B1–B3 are fixed.** Each is a small change: copy and a computed weight for B1–B2, and a body check plus URL normalisation for B3. Each needs its tests updated.

The code is well structured, tested at the level of behaviour, and honest about most awkward states. The remaining problems are mostly the site faithfully showing data that the pipeline produced oddly (partial runs, an earlier model's low-coverage scores). It has the information to warn about them and mostly doesn't (S1, S2, S8).

These are **not** merge blockers, but they **are** launch blockers:
- S1, S2 and S8 on today's data
- S4 (contrast of dates and disclaimer)
- S6 and the compliance flags
- M2 (security headers)

Sources for the SEBI points:
- [SCC Online: SEBI Research Analyst (Third Amendment) Regulations, 2024](https://www.scconline.com/blog/post/2024/12/19/sebi-research-analyst-third-amendment-regulations-2024/)
- [Business Today: SEBI enhances research analyst regulations, releases FAQs (Jul 2025)](https://businesstoday.in/markets/stocks/story/sebi-enhances-research-analyst-regulations-releases-faqs-for-clarity-486103-2025-07-24)
- [Lakshmikumaran & Sridharan: key clarifications under the SEBI FAQs 2025](https://lakshmisri.com/insights/articles/key-clarifications-under-the-sebi-issued-faqs-2025)

The lawyer should read the current regulation text on sebi.gov.in rather than these summaries.
