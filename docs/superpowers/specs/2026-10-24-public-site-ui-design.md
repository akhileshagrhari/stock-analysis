# Public site UI: design

**Date:** 2026-10-24 · **Status:** approved direction, ready to plan · **Branch:** `claude/ui-design-c663e2`

Visual reference: [`assets/2026-10-24-stock-page-mock.html`](assets/2026-10-24-stock-page-mock.html). This is the approved mock of the stock page with RELIANCE's real API data embedded. Open it in a browser and use the Desktop/Phone switch. It is a throwaway prototype: copy its look, not its code.

---

## 1. Intent

StockAnalysis already scores every NIFTY100 company on factor families (value, quality, growth, momentum, sentiment) and turns that into a composite score and a BUY / HOLD / SELL signal with reasons. The public site puts that in front of investors.

**Who visits.** Someone with a stock in mind, trying to decide whether it is good to buy or good to sell. They know fundamentals. "Book-to-price is 15 standard deviations above its sector" means something to them. Show real factor names, raw values, sector z-scores and percentiles. The plain-English meanings the API returns ("cheap on book value") are secondary text, not a replacement.

**What they must leave knowing.**
1. The verdict: score and signal.
2. *Why*: which factor families and which individual factors pushed the score up and which pulled it down.

The "why" is the centrepiece of the page, not a footnote.

**What sets it apart from Tickertape.** The owner uses Tickertape and likes its clean layout and scorecards. The problem with it is that it forgets. A month after buying on its analysis you can't see what the score was when you bought, or whether it has improved since. Our site keeps **score history**. Anyone can scroll back to the date they bought and compare that day with today. No login is needed. Saving a purchase date per user comes later, with accounts.

## 2. Goals, non-goals, anti-goals

**Goals (v1):**
- Three pages: **stock page** (`/stocks/RELIANCE`), **home** with search and autocomplete, and a **signals list**. In that order of priority.
- Works well on a phone and on desktop.
- Every number carries the date it is true as of.
- Built against the real `/v1` API only, with no placeholder data in the product.
- The stock page leaves room for a whole-market screener later, through a sidebar slot and the same card system.

**Non-goals (later, not v1):**
- Login, watchlist, portfolio, saved purchase dates, alerts.
- The screener.
- Dark mode. The design tokens should make it easy later.
- Company logos and descriptions.
- A compare-two-stocks page.
- Any backend change. See §9.

**Anti-goals.** It would be a failure if any of these happens, even if everything technically works:
- The page reads as personal advice to the visitor.
- A number appears with no date.
- Missing data looks like good data. Examples: an unscored bank showing a 0, unchecked red flags reading as "clean", an unscored news item counted as neutral.
- Scores from different model versions are compared without saying so.
- Renaming BUY / HOLD / SELL later needs a redesign.

## 3. Constraints

**Compliance (the owner's decision, recorded).** Publishing buy/sell calls on specific stocks to the public in India generally requires SEBI Research Analyst registration. The owner knows this and has chosen to keep the **BUY / HOLD / SELL** wording for now. They will take legal advice before public launch, and the label may change, perhaps to Good / Average / Bad. So:
- The three labels, their colours and their one-line meanings live in **one config module**. Changing the wording is a one-file edit.
- Every page that shows a score carries the disclaimer component (§6.6), with text marked *pending legal review*.
- Copy describes the model's output ("our model ranks it…"). It never addresses the visitor ("you should sell").

**No backend changes.** The API is read-only and fixed for this work. Where the design wants data the API doesn't return, the page degrades gracefully and the gap goes into §9.

**Data as it stands today (2026-10-24).** These states are real and the UI must handle all of them:
- **Stale prices.** Prices end **14 Aug 2026**. The pipeline hasn't been re-run since.
- **Partial latest run.** The latest scored date is **16 Aug 2026**, but only **4** companies were scored that day (RELIANCE, ABB, ADANIGREEN, ADANIENSOL). That looks like a partial run. `/v1/signals` and `/v1/health` treat 16 Aug as "latest", so the signals list shows 4 rows by default. 74 more companies were last scored on **14 Aug 2026**.
- **Low coverage.** Most companies' latest scores have **coverage 0.23**, meaning data backs 23% of the model's weight. Coverage is weighted by family and by each factor's weight within its family, so it is not a share of factors. Only the 16 Aug four have 0.65–0.90.
- **Unscored companies.** **22 of 100** instruments have no signal at all: `/latest` and `/explanation` return 404 "No signal stored". These are mostly banks, NBFCs and insurers (HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK, BANKBARODA, CANBK, PNB, UNIONBANK, HDFCLIFE, SBILIFE, BAJAJHLDNG, MUTHOOTFIN, SHRIRAMFIN, TATACAP, RECLTD), plus GAIL, HYUNDAI, LODHA, ULTRACEMCO, TMCV and ENRIN. Banks also have no annual financials, only quarterly revenue, PAT and EPS.
- **Short history.** Each company has 1–4 snapshots (31 Jul, 14 Aug, 15 Aug, 16 Aug). The model version changed between 14 and 16 Aug (`…a8756fe2` → `…4b05e87d`).

## 4. Decisions the owner made

| Topic | Decision |
|---|---|
| Audience | Fundamentals-literate investors. Show the technical numbers. |
| Signal wording | Keep BUY / HOLD / SELL for now, configurable in one place. |
| Score memory | Scroll back through the history and compare with any past date. No login in v1. |
| Look and feel | The pattern of the reference the owner supplied (a dashboard-style stock page). Left sidebar nav, light grey canvas with white rounded cards, purple accent, a large price card with period toggles, gauge-style check cards, and a three-column bottom row. Realised in the approved mock. "Looks good, we'll improve once the basics are set." |
| Process | Plan first → owner compacts the session → owner asks for the code. |

## 5. Pages

The app shell is shared. On desktop: a left **sidebar** with brand, Home, Stocks, Discover (Signals, Red flags*, Sectors*, Screener *soon*), Learn (How scores work*, Model & weights*, Disclaimer), and a **data freshness** card. A **top bar** has breadcrumbs and a search box (⌘K / Ctrl-K focuses it). On a phone: a top bar with menu, brand and search icon. The sidebar becomes a drawer.

\* These links may point to simple placeholder pages in v1. Red flags can be the signals list filtered to flagged names via `/v1/signals/red-flags`. "Model & weights" can render `/v1/model`. The builder decides how far to take these. Signals, Home and Disclaimer are real.

### 5.1 Stock page: `/stocks/[symbol]` (the priority)

Resolve `symbol` with `/v1/instruments/by-symbol/{symbol}` and treat it as case-insensitive (canonical URL is upper-case). Unknown symbol → a 404 page with search.

**Header:** a monogram circle (initials, since there are no logos), the NSE symbol large, the company name, a sector pill, an "NSE · NIFTY100" pill. On the right: a disabled "☆ Watchlist *soon*" and a primary "How this score works" (links to Learn).

**Section tabs:** Overview · Score breakdown · Score history · Financials · News · Peers. In v1 this is **one long page** and the tabs are in-page anchors with a scroll-spy underline. On a phone the tab strip scrolls horizontally.

**Overview grid.** Each card below is listed with its counterpart in the reference.

| Card | Reference analog | Content | Source |
|---|---|---|---|
| **Composite score** | Win rate | `28 / 100`, signal chip, a segmented 0–100 meter (20 segments; zones SELL < 45, HOLD, BUY ≥ 75 from `/v1/model` thresholds) with a ▼ marker, "Scored 16 Aug 2026 · model 4b05e87d", and an ⓘ tooltip: *relative rank among scored NIFTY100 stocks on one date, not a valuation or price target*. | `/explanation` (+ `/v1/model` for thresholds) |
| **Score trend** | Total return | Change since first scored (`−4.1`), a sparkline of every snapshot (dots, purple area), a dashed **"model updated"** marker wherever `model_version` changes, "N snapshots · scroll back in Score history". | `/history?limit=500` |
| **Price** | Price chart | Last close large, change pill and absolute change for the selected period, "NSE close · 14 Aug 2026". Period toggles 1M 3M 6M 1Y 3Y Max (Max = all stored, back to Aug 2020). Line chart with right-hand axis, crosshair and tooltip. Stats row: 52-week high and low, % off the 52-week high, last volume. | `/prices?from=…` |
| **Checks: news tone** | Sentiment gauge | Half-gauge for the 30-day mix, the label (Positive / Mixed / Negative), counts "1 positive · 1 negative · 1 neutral", and "Too few articles to score sentiment" when the sentiment family is not measured. `news: null` means *no scored news*, which is different from neutral. | `/explanation.news`, `.families` |
| **Checks: red flags** | Sentiment gauge | Count triggered (big number), then one chip per flag in the model's six: ✓ checked and clear, ✗ triggered (red), **?** couldn't be checked (dashed). Footer: "Unchecked ≠ clean". | `/explanation.red_flags`, `.unknown_flags` + the frontend flag catalogue (API requirement R3) |
| **Why this score** | Trade idea | Signal chip and as-of date. A grey panel with a one-line summary built from family verdicts ("Value pulls the score up; quality and momentum pull it down"), badges for the strongest and weakest families with percentiles, and a **coverage pill** ("Data behind 90% of the model's weight · 19 of 20 factors"). Underneath, a **scale bar** 0–100 with ticks at the SELL and BUY lines, the score as a dot, and a "17 pts to HOLD" / "6 pts to BUY" pill (the stop/entry/target analog). If `narrative` is non-null, show it here. | `/explanation` |

**Bottom row (three columns on desktop):**
- **Score breakdown:** one row per family with a diverging contribution bar (zero in the middle, green right, red left) and its value, percentile, and the weight the family actually carried: its declared weight re-normalised over the measured families (CHOLAFIN growth "57% of 20%"), as the API computes contribution. A family that wasn't measured gets a hatched bar, "n/m" and "—" for weight. Footnote: "Percentile within the scored universe on {date}. Contribution = family z × weight, re-weighted over the measured families. {Unmeasured families} weren't measured, so the others were re-weighted."
- **Factor drivers:** segmented control *Pulling up / Pulling down*. Each item has a z pill ("+15.2 SD vs sector"), the factor label, the raw value formatted by unit, the meaning line and the family tag. Show the top 4 by default with "show all".
  - **Meaning line.** The API's `z` is *sign-adjusted*: positive is always good for the company, whichever way the raw metric runs. `meaning` describes a positive z ("carries less debt" for debt/equity, "stronger price trend" for momentum). So render it as **"Positive = {meaning}"** in both lists. Never "High = …", which would be wrong for debt/equity and accruals, and never "Weak: stronger price trend".
  - **Units.** Until R3 in the API requirements lands, raw-value units come from a frontend map, `web/src/config/factors.ts`. Verify each one against `src/stockanalysis/factors/*.py` while building:
    - As a percent with one decimal: `earnings_yield`, `ebitda_to_ev`, `fcf_yield`, `roe`, `roce`, `revenue_cagr_3y`, `pat_cagr_3y`, `quarterly_revenue_yoy`, `quarterly_pat_yoy`, `momentum_12_1`, `relative_strength_6m`, `accruals`.
    - As percentage points: `margin_trend`.
    - As a multiple with two decimals: `book_to_price`, `debt_to_equity`, `cfo_to_pat`, `interest_coverage`, `peg_inverse`.
    - As "% vs trend" (raw − 1): `price_to_200dma`.
    - As a −1…+1 score: `news_sentiment_30d`.
    - Unknown names fall back to two decimals.
- **News:** segmented control *Scored / All N*. Each item has a sentiment pill ("Positive +0.93", or "Not scored yet" when `label` is null), the date, the headline linking out (`rel="noopener"`, new tab) and the source.

**Score history section (the differentiator):**
- A larger chart of every snapshot: score on y (0–100), shaded SELL/HOLD/BUY bands, model-change markers, coverage shown on hover.
- A **"Compare with…"** date picker over the snapshot dates. It shows *then vs now* side by side: score, signal, coverage, red flags and unknown flags, and model version. If the model versions differ, show a "not strictly comparable" warning.
- A table of all snapshots, newest first.
- *Family-level then-vs-now needs a past-date explanation, which the API doesn't have (API requirement R1).* Leave visible room for it, for example a "Family changes: coming soon" row. Don't fake it.

**Financials section:**
- Segmented *Quarterly / Annual*. Tables scroll horizontally on a phone with a sticky first column.
- Quarterly (up to 12): revenue, PAT, PAT margin (derived), EPS, and a "Filed" row.
- Annual: all line items in `line_items` order. A null value renders "—" with the tooltip "Not in the filing". The last rows are derived Debt/Equity and a "Filed · auditor" row.
- Column labels come from the period end: "Jun '26" for quarters. For annual, "FY26" for a March year-end, otherwise "FY25 (Dec)".
- Header note: "₹ crore · consolidated/standalone per `basis` · each column dated by when it was filed". The unit is ₹ crore (₹ per share for EPS), as `LineItemOut` documents in code; R17 asks for it in the schema.
- Banks: no annual rows → show "No annual figures in this company's filings" instead of an empty table.

**Peers section:** same-sector companies scored on the same date (`/v1/signals?sector=…&as_of=…`). Columns: symbol and name, score, signal, coverage. The current company is highlighted. If no other company in the sector was scored that day (true for RELIANCE on 16 Aug today), say so: "No other {sector} company was scored on {date}". Don't silently mix in another date. Unscored companies show peers from the latest scored date, because they have no date of their own.

**Red-flag catalogue.** Until R3 lands, the six flags live in `web/src/config/redflags.ts`, copied from `src/stockanalysis/factors/redflags.py::DEFINITIONS`:
- `promoter_pledge`: Promoter pledge > 25% of promoter holding. No data source.
- `auditor_qualification`: Auditor qualification, adverse opinion or disclaimer.
- `weak_cash_conversion`: CFO/PAT < 0.5 for 3 consecutive years.
- `promoter_selling`: Promoter holding falling 3 consecutive quarters.
- `contingent_liabilities`: Contingent liabilities > 50% of net worth.
- `rating_downgrade`: Any credit rating downgrade. No data source.

A flag is **clear** when it is in neither `red_flags` nor `unknown_flags`. Prefer `red_flags[].description` from `/explanation` when it is present.

**Phone order:** score → why → price → score trend → checks → breakdown → drivers → news → score history → financials → peers. The mock shows this.

### 5.2 Home: `/`

- **Hero search:** "Search NIFTY100 stocks". Autocomplete via `/v1/instruments/search?q=` from the browser, debounced (~150 ms), with keyboard navigation (↑ ↓ Enter Esc). Show symbol, name and sector. Enter on a symbol goes to its page.
- **Today's picture:** for the latest scored date, count BUY / HOLD / SELL, plus "N not scored" (`/v1/health.instruments` minus rows with a score). State the date.
- Short lists: **highest scores** (top 5), **lowest scores** (bottom 5), **red flags** (from `/v1/signals/red-flags`). Each row links to the stock page.
- A **"How scores work"** card: families and weights from `/v1/model`, thresholds, and "percentiles, not valuations".
- The disclaimer.

### 5.3 Signals list: `/signals`

- **Filters:** a signal segmented control (All / BUY / HOLD / SELL), a sector select (`/v1/sectors`) and a **scored-date select** (`/v1/model.scored_dates`, default latest). Filters live in the URL query (`?signal=BUY&sector=…&as_of=…`) so views can be shared.
- **Desktop table**, sortable client-side:
  - Stock (symbol and name), sector, score (number plus a thin bar), signal chip, coverage (amber under 50%).
  - Red flags: count of triggered flags, plus "?N" for unknown.
  - Change since the previous scored date: computed by fetching `/v1/signals?as_of=<previous date>` and joining on ISIN. Blank if the company wasn't scored then, with a dagger if the model version differs.
- **Phone:** a card per row.
- Rows with a null score (below the coverage floor) sort to the bottom labelled "Not scored".
- Header line: "{N} stocks scored on {date}". When the selected date scored fewer than half the instruments (today: 4 of 100), show a notice: *"Only 4 of 100 companies were scored on 16 Aug 2026. The most recent fuller list is 14 Aug 2026 (77 companies)."* The notice links to that date. This is how the partial-run problem shows today (§3, API requirement R4).

## 6. Cross-cutting behaviour

### 6.1 Dates on everything
Every score shows its scored date (`as_of`). Every price shows its close date. Every financial column shows its filing date. News shows its publish date. The sidebar **freshness card** shows "Scores: {health.latest_as_of}". On a stock page it also shows that company's last price date and latest filing date. Any date older than **7 days** (my call; the builder may tune it) gets an amber dot.

### 6.2 Awkward states: each must be handled and tested
Each state has a real example company (fixtures, §7).

| # | State | Example | Behaviour |
|---|---|---|---|
| 1 | Scored, ordinary | RELIANCE (SELL 28, coverage 0.90), ABB (BUY 93) | As in the mock |
| 2 | **Unscored**: `/latest` and `/explanation` 404 | HDFCBANK | The score card shows "Not scored" in grey, never a 0. Copy: "Our model hasn't scored this company." For Financial Services add "The filings we read for banks and NBFCs don't carry several figures the model needs." Hide the trend, why, breakdown, drivers and checks cards. Keep the price, financials (quarterly only), news and peers. |
| 3 | **Below coverage floor**: a signal row exists with null score and null signal | none today, but the API allows it (`SignalOut` comment) | "Not scored on {date}: data behind only {coverage}% of the model's weight." |
| 4 | **Red-flag override**: SELL while score ≥ 45 | CHOLAFIN (87 → SELL, promoter_selling), ADANIENSOL (auditor_qualification) | The score shows its real number. A red banner on the score and why cards: "SELL forced by red flag: {description}. Without it, the score would read {HOLD/BUY}." Use the `headline` / `red_flags[].description` from `/explanation`. |
| 5 | **Low coverage** (< 50%) | most companies (0.23) | The coverage pill turns amber: "Data behind 23% of the model's weight (5 of 20 factors), treat with caution". Families with `factors_measured == 0` show as n/m. |
| 6 | Family not measured | RELIANCE sentiment; CHOLAFIN value, quality and sentiment | Hatched bar and "n/m". The breakdown footnote names them. |
| 7 | Unknown red flags | every company today | "?" chips. Never counted as clear. |
| 8 | News sentiment pending (`label: null`) or `news: null` | RELIANCE's 7 newest; ABB has no scored news | "Not scored yet" pill. Gauge card: "No scored news in 30 days", without a needle. |
| 9 | **Stale explanation** (`stale: true`) | CHOLAFIN (stored under `…a8756fe2`, current `…4b05e87d`) | A notice on the breakdown: "This breakdown is re-derived with current weights; the stored score came from model {stored_version}." |
| 10 | Model change inside history | RELIANCE, ABB | Dashed marker and a "not strictly comparable" note in the compare view |
| 11 | One or zero snapshots | ADANIENSOL (1) | 1: a single dot and "First scored {date}". 0: the trend card is hidden. |
| 12 | Odd snapshot | ABB 15 Aug (HOLD 50.0, a partial run) | Shown as is. No special handling in v1. Noted in §10. |
| 13 | Null financial values | RELIANCE EBITDA, contingent liabilities | "—" with the tooltip "Not in the filing" |
| 14 | Non-March fiscal year | ABB (Dec year-end) | "FY25 (Dec)" column labels |
| 15 | Quarter newer than prices | HDFCBANK Sep 2026 quarter, filed 17 Oct 2026 | Fine: each column carries its own filing date |
| 16 | Partial latest date | 16 Aug 2026 (4 rows) | The signals-list notice in §5.3. Home counts say "on {date}" so the small number is explained. |

### 6.3 Signal labels in one place
`web/src/config/signals.ts` (or similar) exports, per signal key, the display label, tone colour tokens and a one-line meaning. Nothing else in the codebase writes "BUY", "HOLD" or "SELL" as display text.

### 6.4 Visual system (from the mock)
| Element | Value |
|---|---|
| Font | Inter, via `next/font` |
| Canvas | `#F5F5F8` |
| Cards | white, radius 18px, hairline shadow |
| Ink | `#16161D` / `#55556A` / `#8C8C9C` |
| Accent | purple `#6B3CF0` |
| Price line | blue `#2B59E8` |
| Positive | `#0F7B57` on `#E5F5EE` |
| Negative | `#C9302C` on `#FDECEA` |
| HOLD | `#A15C00` on `#FFF3DE` |

- Signal and status colours always come with text, never colour alone.
- Tabular numerals for figures.
- Indian digit grouping (`en-IN`: 10,75,675) and ₹.
- Define colours as CSS custom properties so dark mode is a token swap later.

**Responsive breakpoints** (as in the mock):
| Width | Layout |
|---|---|
| ≥ 1560 px | Four-column overview |
| 1180–1560 px | Price card beside the score cards, checks as a band |
| 720–1180 px | Two columns |
| < 720 px | Phone, single column |

### 6.5 Charts
Hand-rolled SVG, as in the mock: the price line with crosshair and tooltip, the score sparkline, the score-history chart, the half-gauge, the segmented meter and the diverging bars. Each chart has an accessible name and a text summary. The score-history chart has its table as a fallback. No chart library is needed for v1. The builder may choose one if the price chart grows (candles, volume).

### 6.6 Disclaimer
A footer component on every page:

> **Not investment advice.** Scores and BUY / HOLD / SELL labels are outputs of a quantitative model that ranks NIFTY100 stocks against each other on a given date. They are not a recommendation to buy or sell any security and don't account for your circumstances. Data can be incomplete or late; check filings before acting. *[Final wording pending legal review.]*

The "Disclaimer" page holds the long form, which is a placeholder until legal review.

## 7. How it's built (my calls)

These are my decisions, not the owner's. The owner may override them.

- **Next.js (App Router) + TypeScript** in a new top-level `web/` folder, using the current stable versions at scaffold time, pinned exactly. Node is already on the machine (v26, npm 11). npm, no pnpm.
- **Data fetching.**
  - Server components fetch the FastAPI server-side from `SA_API_URL` (default `http://localhost:8000`), with `revalidate: 300` to match the API's `Cache-Control`.
  - Browser-side calls go to `/api/v1/…`. Those are search autocomplete, chart period changes if not preloaded, and the compare picker if not preloaded.
  - A Next.js **route handler** (`web/src/app/api/v1/[...path]/route.ts`) proxies the browser's calls to `${SA_API_URL}/v1/...`, reading `SA_API_URL` at request time (a `next.config` rewrite would fix it at build time). It forwards only what browser code calls (search) and times out after 5 seconds. So the browser never talks to the API's origin, and `SA_API_CORS_ORIGINS` can stay empty with no backend change.
- **Typed client.** `stockanalysis export-openapi web/openapi.json`, then `openapi-typescript` generates `web/src/lib/api/schema.d.ts`. Commit both. A thin `api.ts` wraps fetch, returns typed results, and maps 404 to `null` so "unscored" is a value, not an exception.
- **Styling:** CSS Modules plus a global tokens file. This ports straight from the mock's CSS, with no utility-framework dependency.
- **Price data:** the stock page fetches the full stored range once on the server (`from=2000-01-01`, about 1,500 bars) and passes a compact `[date, close][]` to the client chart, which slices by period locally.
- **Tests.**
  - Vitest + Testing Library for the logic and states: formatters (₹, en-IN, %, z), fiscal-year labels, the state derivation in §6.2, the summary-line builder, and the signals join.
  - **Fixtures** are real API responses captured into `web/tests/fixtures/` for RELIANCE, ABB, HDFCBANK, CHOLAFIN and ADANIENSOL, plus `/v1/signals` for 14 and 16 Aug, `/v1/model` and `/v1/health`.
  - A small **Playwright** smoke suite loads `/`, `/stocks/RELIANCE`, `/stocks/HDFCBANK`, `/stocks/CHOLAFIN` and `/signals` at 1440 px and 390 px against the running local API, and checks there are no console errors.
- **Launch config.** Add a `web` entry to `.claude/launch.json` (`npm run dev` in `web/`, port 3000) beside the existing `api` one.
- **Brand name.** "StockAnalysis" is a placeholder. It lives in one config constant, alongside the signal labels.
- **Partial latest date.** Home and the signals list default to the API's latest date and *explain* the small count, with a link to the previous date. They don't silently switch to a fuller date. Revisit when R4 lands or the pipeline re-runs fully.

**Left to the builder:**
- Component breakdown and file layout inside `web/`.
- Exact copy beyond what's quoted here.
- How the placeholder Learn and Discover pages look.
- Whether ⌘K opens a palette or just focuses the search.
- Animation.
- Whether to preload all history for the compare picker (it's small, ≤ 500 rows).

## 8. Out of scope for this work
Backend or API changes, pipeline re-runs, data fixes, deployment and hosting, analytics, SEO beyond sensible `<title>` and meta per page, and authentication.

## 9. API gaps for a follow-up

These are written up for the backend worker in **[`2026-10-24-site-api-requirements.md`](2026-10-24-site-api-requirements.md)**. Each requirement there has its route, fields, why, a real example, a "done when" check and the site's fallback. None of them blocks v1.

**P1:**
- **R1:** explanation for a past date
- **R2:** why a company isn't scored
- **R3:** model catalogue (flags, factor units, coverage floor)
- **R4:** latest-per-instrument signals and run completeness
- **R5:** previous score on every signal row

**P2:**
- **R6:** shareholding
- **R7:** key metrics consistent with the factors
- **R8:** corporate actions and adjusted EPS
- **R9:** richer quarterly results
- **R10:** filing links
- **R11:** per-family history
- **R12:** news relevance and sentiment status
- **R13:** industry and profile
- **R14:** freshness in health

**P3:**
- **R15:** score on search results
- **R16:** sector context per factor
- **R17:** units in the financials schema

## 10. Data issues noticed (pipeline, not UI)

Reported for the owner. These are out of scope here.

1. **RELIANCE's value factors use FY2024 pre-bonus per-share figures against a bonus-adjusted price.** `earnings_yield` 0.07855 = FY24 EPS 102.90 ÷ ₹1,310. `book_to_price` 1.0445 = FY24 equity ÷ 676.6 cr pre-bonus shares ÷ ₹1,310. That's despite FY26 figures having been filed on 24 Apr 2026. `book_to_price` (+15.2 SD) is RELIANCE's **top** stated reason. Details are in the API requirements doc, D1 and D4.
2. **A partial run on 16 Aug** (4 companies) and a **single-company row on 15 Aug** (ABB, HOLD 50.0) sit in `signals` as if they were full runs.
3. **Coverage is 0.23 for most companies' latest score.** Only the 16 Aug four were scored with fundamentals.
4. **News quality.** The newest items aren't sentiment-scored yet. Some items tagged to a company are unrelated market roundups, for example a "stocks to buy" article about other companies scored negative for RELIANCE.
