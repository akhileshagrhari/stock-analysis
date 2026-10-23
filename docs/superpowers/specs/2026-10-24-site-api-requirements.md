# API requirements from the public site

**For:** the backend worker (FastAPI `serve/api.py`, `serve/queries.py`, `serve/explain.py`, `db/publish.py`) · **From:** the public-site UI design · **Date:** 2026-10-24

The site is designed in [`2026-10-24-public-site-ui-design.md`](2026-10-24-public-site-ui-design.md) and its build plan is in `docs/superpowers/plans/2026-10-24-public-site-ui.md`. The site launches on the API as it is today. Every item below has a frontend fallback until it lands, so none of them blocks the first build. Each item says what the site would show if it had the data.

**Priorities:**
- **P1:** the stock page is incomplete or less honest without it.
- **P2:** a standard feature investors expect.
- **P3:** nice to have.

## Ground rules (from the existing API and the Postgres hand-over notes)

- **Additive only in `/v1`.** New optional fields and new routes are fine. Never rename, remove or retype a field. A breaking change gets `/v2` beside `/v1`, as the `api.py` docstring requires.
- Every new GET route needs a URL in `tests/test_postgres_serving.py::PARITY_URLS`. The parity test needs byte-identical JSON from DuckDB and Postgres.
- A route that reads a table not in `db/publish.py::SITE_TABLES` must add it there. `corporate_actions` and `filings` aren't published today.
- SQL must run on both engines. Alias every subquery in `FROM`. Don't use DuckDB-only functions (`QUALIFY`, `FIRST(… ORDER BY)`, list functions).
- Nulls stay null, never NaN. Dates are `YYYY-MM-DD`. Every value that can go stale carries its own as-of or knowledge date (filing, disclosure or price date).
- Regenerate the schema the site builds its types from: `stockanalysis export-openapi web/openapi.json`. Field descriptions in the Pydantic models end up in OpenAPI, so state units and null meanings there.

---

## P1

### R1. Explanation for a past date
- **Route:** `GET /v1/instruments/{isin}/explanation?as_of=YYYY-MM-DD`. `as_of` is optional and defaults to the latest, as today.
- **Response:** the existing `ExplanationOut`, computed from that date's `signals` row and `factor_scores`. The same `stale`, `stored_version` and `current_version` semantics apply when that date's row was stored under another model.
- **Errors:** 404 when the instrument has no signal on that date. Don't fall back to the nearest date.
- **Why:** the site's differentiator is "score memory". A holder scrolls back to the day they bought and compares it with today. Today the comparison can only show score, signal and coverage. With R1 it shows *which families and factors moved* (for example "Momentum 41st → 21st").
- **Example:** `…/INE002A01018/explanation?as_of=2026-07-31` returns RELIANCE's breakdown as stored on 31 Jul (score 32.5, model `…a8756fe2`).
- **Done when:** a past date returns that date's families, strengths and weaknesses; a date with no row returns 404; the parity URL is added.
- **Fallback today:** the compare panel shows score, signal, coverage and flags only, with "Family changes: coming soon".

### R2. Why a company isn't scored
- **Route:** `GET /v1/instruments/{isin}/score-status`. It returns 200 for any known ISIN and 404 only for an unknown ISIN.
- **Response:**
  - `scored`: bool
  - `latest_as_of`: date | null (the latest date with a signal row)
  - `reason`: an enum, at minimum `scored`, `never_scored`, `below_coverage_floor`, `not_in_latest_run`. Add values like `missing_fundamentals` or `sector_not_supported` if the pipeline can tell.
  - `coverage`: float | null
  - `coverage_floor`: float
  - `detail`: str | null, one plain sentence the pipeline can stand behind, such as which inputs were missing and how many factors that left unmeasurable.
- **Why:** 22 of 100 instruments have no signal row at all: HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK, BANKBARODA, CANBK, PNB, UNIONBANK, HDFCLIFE, SBILIFE, BAJAJHLDNG, MUTHOOTFIN, SHRIRAMFIN, TATACAP, RECLTD, GAIL, HYUNDAI, LODHA, ULTRACEMCO, TMCV, ENRIN. `/latest` and `/explanation` answer 404 "No signal stored", so the page can only guess why. Several of these (GAIL, HYUNDAI, ULTRACEMCO) aren't financials, so the bank explanation the site uses as a fallback is wrong for them.
- **Done when:** each of the 22 returns a specific `reason` and `detail`; a scored company returns `scored: true` with its coverage.

### R3. Model catalogue in `/v1/model`
- **Add to `ModelInfoOut`:**
  - `version`: str (the current `model_version`)
  - `coverage_floor`: float
  - `families: [{family, meaning, weight}]`. `family_weights` stays for compatibility.
  - `factors: [{name, label, meaning, family, unit, higher_is_better}]`. `label` and `meaning` come from `explain.FACTOR_LABELS`. `unit` is an enum: `percent` (fraction to show as %), `pp` (percentage points), `multiple` (×), `ratio_vs_trend` (price / 200-DMA), `score` (−1…+1). `higher_is_better` is the raw metric's direction before the sign flip.
  - `red_flags: [{name, description, reachable}]` from `factors/redflags.py::DEFINITIONS`.
- **Optional mirror:** `unit` on `DriverOut` and `FactorOut`.
- **Why:**
  - The site must otherwise hard-code copies of `DEFINITIONS` and `FACTOR_LABELS`, and guess units. Is `0.0785` 7.85% or 0.0785×? It shows raw values to fundamentals-literate visitors, so a wrong unit is a visible error.
  - The six-flag checklist ("✓ clear / ✗ triggered / ? couldn't check") needs the full list, because a flag is clear only by absence from `red_flags` and `unknown_flags`.
- **Done when:** the lists match the Python sources exactly, and a test fails if a factor or flag is added in Python without being exposed here.

### R4. Signals list that isn't hostage to a partial run
- **Route:** `GET /v1/signals?latest=per_instrument`, combinable with `signal` and `sector`. It returns each instrument's newest signal row, whatever its date. `as_of` is per row, as today.
- **Also:** add `scored_date_counts: [{as_of, scored, instruments}]` to `/v1/model`. In `/v1/health`, keep `latest_as_of` and add `latest_complete_as_of`: the newest date on which at least a defined share of active instruments was scored. Say the share in the field description.
- **Why:** `/v1/signals` uses `MAX(as_of_date)` across all companies. The 16 Aug 2026 run scored only 4 companies (RELIANCE, ABB, ADANIGREEN, ADANIENSOL), so the default list, the home-page counts and `health.latest_as_of` all describe 4 companies. Meanwhile 74 others were last scored on 14 Aug. Until this lands, the site shows "Only 4 of 100 companies were scored on 16 Aug 2026" and links to 14 Aug. Mixing dates in one ranking is a product question; per-row dates make it visible.
- **Done when:** `latest=per_instrument` returns 78 rows on today's data (every instrument with any signal), each with its own `as_of`.

### R5. Previous score on every signal row
- **Add to `SignalOut`:** `previous: {as_of, composite_score, signal, model_version} | null`, the same instrument's most recent row strictly before this row's `as_of`. This covers `/signals`, `/latest` and `/history`.
- **Why:**
  - It gives the signals list's "change since last scoring" column and the home page's "biggest movers", without a second list call and a client join.
  - The join today only works when the previous scored date was a full run.
- **Done when:** ABB on 16 Aug returns `previous.as_of = 2026-08-15`, `previous.composite_score = 50.0`. RELIANCE on 16 Aug returns `previous.as_of = 2026-08-14`.

---

## P2

### R6. Shareholding pattern
- **Route:** `GET /v1/instruments/{isin}/shareholding?quarters=12`.
- **Response:** a list, newest first, of `{quarter_end, disclosed_date, promoter_pct, promoter_pledged_pct, fii_pct, dii_pct, public_pct}`.
- **Why:**
  - It's a standard Indian stock-page section.
  - It gives context for the `promoter_selling` red flag. CHOLAFIN is forced to SELL by it, and the page should show the three falling quarters.
- **Note:** the `shareholding` table is already published to Postgres for this ("the site's next pages").

### R7. Key metrics consistent with the value factors
- **Route:** `GET /v1/instruments/{isin}/key-metrics`.
- **Response:** `{as_of, price, market_cap_cr, shares_outstanding_cr, eps_ttm, pe_ttm, book_value_per_share, pb, dividend_yield, high_52w, low_52w}`. Each figure is null when unknown, and the response gives the knowledge date it rests on (price date, filing date).
- **Why:** investors expect market cap, P/E and P/B. They must use the *same* share count and price basis as `earnings_yield` and `book_to_price`. Otherwise the page contradicts itself, which it does today (see data issue D1).
- **Prerequisite:** no stored table has shares outstanding. The source is the backend's call.

### R8. Corporate actions and adjusted EPS
- **Route:** `GET /v1/instruments/{isin}/corporate-actions`.
- **Response:** `[{ex_date, action_type, ratio, details}]`. The `corporate_actions` table exists but isn't published or routed.
- **Also:** add `eps_adjusted` to the quarterly and annual rows, adjusted to today's share basis.
- **Why:** RELIANCE's quarterly EPS halves at Dec 2024 (Sep '24: 24.48 → Dec '24: 13.70) because of the 1:1 bonus. The EPS row isn't comparable across that break, and the site can't detect it.

### R9. Richer quarterly results
- **Add to `QuarterOut`, where the quarterly XBRL carries them:** `total_income`, `other_income`, `total_expenses`, `operating_profit` (or `ebitda`), `depreciation`, `interest_expense`, `profit_before_tax`, `tax_expense`, `basis`.
- **Why:** quarterly rows have only revenue, PAT and EPS, which is thin next to annual figures for an audience that reads results.
- **Note:** this needs the ingest to store the fields. `fundamentals_quarterly` has only three value columns.

### R10. Filing links
- **Add:** `source_url` to each annual and quarterly row, from the `filings` table, which must be published.
- **Why:** the disclaimer says "check filings before acting". Each financial column should link to the NSE filing it came from.

### R11. Per-family history
- **Add to `/history` rows:** `families: [{family, percentile, contribution}]`.
- **Why:** per-family trend lines in Score history without one R1 call per date.

### R12. News relevance and sentiment status
- **Add to `NewsOut`:**
  - `relevance`: `primary` | `mention` | null
  - `sentiment_status`: `scored` | `pending` | `failed`
  - an optional `?relevant_only=true` filter
- **Why:**
  - Market roundups that merely list a company count toward its 30-day tone. A "stocks to buy" piece about Bank of Maharashtra and Hindustan Copper is scored −0.96 for RELIANCE.
  - The 7 newest RELIANCE items have `label: null`, and the site can't tell "not scored yet" from "scoring failed".

### R13. Instrument profile fields
- **Add to `InstrumentOut`:** `industry` (already in `instruments`, not returned).
- **Populate:** `bse_code` and `listing_date`, which are null for all 100.
- **Later:** `description`, `website`, `logo_url`.
- **Why:** the header pills, an "About" card and logos. The site shows initials until logos exist.

### R14. Freshness in `/v1/health`
- **Add:** `latest_price_date`, `latest_filing_date`, `latest_news_date`, plus R4's `latest_complete_as_of`.
- **Why:** the sidebar's "Data freshness" card. Today the site knows only the latest scored date site-wide.

---

## P3

### R15. Score on search results
Add `signal`, `composite_score` and `as_of` to `/v1/instruments/search` results, so autocomplete rows can show the score chip.

### R16. Sector context per factor
Add `sector_median`, `sector_n` and `sector_percentile` to each `FactorOut` and `DriverOut`. The "+15.2 SD vs sector" pill gets a "sector median 0.42, n = 6" tooltip. This matters because robust z-scores in small sectors can be very large.

### R17. Units in the financials schema
State in the OpenAPI field descriptions, or as `FinancialsOut.unit = "INR crore"` and `per_share_unit = "INR"`, what `LineItemOut`'s code comment already says. The comment reads: rupees per share for per-share lines, ₹ crore otherwise.

---

## Data issues found while designing (pipeline, not API)

**D1. RELIANCE's value factors use FY2024 pre-bonus per-share figures against a bonus-adjusted price.**
- On 16 Aug 2026, `earnings_yield = 0.07855`. That is exactly FY2024 EPS 102.90 ÷ ₹1,310.
- `book_to_price = 1.0445`. That is exactly FY2024 equity ₹9,25,788 cr ÷ 676.6 cr pre-bonus shares (≈ ₹1,368 per share) ÷ ₹1,310.
- Two problems:
  1. FY2026 annual figures (filed 24 Apr 2026) and FY2025 were available, but FY2024 was used.
  2. Per-share figures from before the Oct 2024 1:1 bonus aren't adjusted, while the price series is. Its 2020 closes are around ₹975, half the traded price.
- `book_to_price` (+15.2 SD vs sector) is RELIANCE's top stated reason and drives its value family to the 92nd percentile. Every company with a split or bonus in its history may be affected.

**D2. Partial runs are stored as full dates.** 16 Aug (4 companies) and 15 Aug (ABB alone, HOLD 50.0) sit in `signals` like complete runs and become "latest". See R4.

**D3. Coverage 0.23 for most latest scores.** 74 companies' newest rows were scored with 23% of factors. Only the 16 Aug four had fundamentals in scope (0.65–0.90).

**D4. `prices_daily.close` is documented as "raw traded price" but looks split- and bonus-adjusted.** RELIANCE's 2020 closes are about half the traded price. Either the comment or the data is wrong, and value factors must use the same basis as per-share fundamentals (D1).

**D5. News tagging.** See R12.
