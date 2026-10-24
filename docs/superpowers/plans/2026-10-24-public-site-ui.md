# Public Site UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Next.js public site in `web/` with three pages, a stock page, a home page with search, and a signals list, rendered from the existing FastAPI `/v1` routes in the approved mock's visual style.

**Architecture:**
- Server components fetch the FastAPI server-side from `SA_API_URL`.
- Browser calls go to `/api/v1/*`, which a Next.js rewrite proxies to the API. No CORS or backend change is needed. *(Superseded in the final review: a rewrite's destination is fixed at build time, so the proxy is now a route handler, `web/src/app/api/v1/[...path]/route.ts`, that reads `SA_API_URL` per request and forwards search only.)*
- All view logic is plain, tested functions in `src/lib/**`: state derivation, formatting, joins and period slicing.
- Pages are thin async loaders that pass data into synchronous `*View` components. That keeps the components testable with fixtures captured from the real API.

**Tech Stack:** Next.js (App Router) + TypeScript, CSS Modules + CSS custom-property tokens, `next/font` Inter, `openapi-typescript`, Vitest + Testing Library (jsdom), Playwright (smoke only). npm with exact version pins.

**Spec:** [`docs/superpowers/specs/2026-10-24-public-site-ui-design.md`](../specs/2026-10-24-public-site-ui-design.md). Visual reference: [`docs/superpowers/specs/assets/2026-10-24-stock-page-mock.html`](../specs/assets/2026-10-24-stock-page-mock.html). Open it in a browser and copy its CSS values and layout. Read both before starting.

## Global Constraints

- **No backend changes.** Touch only `web/`, `docs/`, and `.claude/launch.json`. Nothing under `src/`, `tests/` or `pyproject.toml`.
- Data comes from the real API only. The product has no placeholder numbers. Fixtures are for tests only.
- Every displayed number carries its date: score `as_of`, price bar `date`, filing `filing_date`, news `published_at`.
- The display strings "BUY", "HOLD" and "SELL" come only from `web/src/config/signals.ts`. The brand name "StockAnalysis" comes only from `web/src/config/site.ts`.
- Thresholds come from `/v1/model` (`buy_threshold` 75, `sell_threshold` 45 today). Don't hard-code them outside tests.
- Number formatting:
  - Indian digit grouping via `en-IN` (`10,75,675`) and `₹` for money.
  - Tabular numerals for figures.
  - Dates render like `16 Aug 2026`. Parse `YYYY-MM-DD` from its components, never via `new Date(iso).toISOString()`; this exact bug appeared in the mock under IST.
- Responsive breakpoints on the page container:
  | Width | Layout |
  |---|---|
  | ≥ 1560 px | Four-column overview |
  | 1180–1560 px | Three columns, checks as a band |
  | 720–1180 px | Two columns |
  | < 720 px | Phone, one column |
- Tokens, exactly:

  | Token | Value |
  |---|---|
  | `--bg` | `#F5F5F8` |
  | `--card` | `#FFFFFF`, radius 18px |
  | `--ink` / `--ink2` / `--ink3` | `#16161D` / `#55556A` / `#8C8C9C` |
  | `--accent` | `#6B3CF0` |
  | `--price` | `#2B59E8` |
  | `--pos` | `#0F7B57` on `#E5F5EE` |
  | `--neg` | `#C9302C` on `#FDECEA` |
  | `--hold` | `#A15C00` on `#FFF3DE` |

- Copy describes the model ("our model ranks…") and never addresses the visitor ("you should…").
- The disclaimer text is verbatim from spec §6.6, including "[Final wording pending legal review.]".
- Pin exact versions: `web/.npmrc` contains `save-exact=true`. Use the current stable Next.js at scaffold time. Node v26 / npm 11 are on the machine.
- Local API: `.venv/bin/stockanalysis serve-api` on port 8000 (the "api" config in `.claude/launch.json`), reading Postgres.

## Review Focus

These are the inputs most likely to bite users that the feature tests wouldn't naturally cover. Each one has a pinned test in the task named.

1. **Symbols with URL-special characters or the wrong case.** `/stocks/m%26m`, `/stocks/bajaj-auto` and `/stocks/reliance` must resolve to M&M, BAJAJ-AUTO and RELIANCE. Encode the path segment when calling the API. *Task 6, `loadStockPage` tests.*
2. **API unavailable.** A 503 during a publish, or a refused connection, must render a "Data is temporarily unavailable" page with retry, not a crash or a blank 0. Only 404 maps to `null`. *Task 1 (client), Task 6 (`error.tsx`).*
3. **Empty price history.** `/prices` can return `[]`. The price card must say "No price data stored" with no NaN, ∞ or broken SVG. *Task 3 (`priceStats`), Task 6 (PriceCard).*
4. **Search races and blank input.** Whitespace-only input must not call the API, because it returns 422. A slow response for "rel" must not overwrite a newer one for "reli". *Task 4, SearchBox tests.*
5. **Signal row with null score or null percentiles.** Below the coverage floor, every bar, percentile and scale must render "n/m" or "Not scored", never `NaN%` or a marker at 0. *Task 3 (`scoreState`), Task 5 (chart primitives).*

---

## File Structure

```
web/
  package.json · .npmrc · tsconfig.json · next.config.ts · vitest.config.ts · playwright.config.ts · .env.example
  openapi.json                         generated by `stockanalysis export-openapi`
  scripts/capture-fixtures.mjs         pulls real API responses into tests/fixtures/
  src/app/
    layout.tsx · globals.css           shell + tokens
    page.tsx                           home (loader) → HomeView
    stocks/[symbol]/page.tsx           stock (loader) → StockPageView
    stocks/[symbol]/not-found.tsx · error.tsx · not-found.tsx
    signals/page.tsx                   signals (loader) → SignalsView
    learn/page.tsx · disclaimer/page.tsx · red-flags/page.tsx
  src/config/   site.ts · signals.ts · redflags.ts · factors.ts
  src/lib/api/  schema.d.ts (generated) · types.ts · client.ts
  src/lib/      format.ts · prices.ts
  src/lib/stock/  state.ts · history.ts · load.ts
  src/lib/signals/  list.ts · load.ts
  src/components/ui/      Card · SignalChip · Pill · Segmented · InfoTip  (+ .module.css)
  src/components/shell/   AppShell · Sidebar · TopBar · MobileNav · FreshnessCard · SearchBox · Disclaimer
  src/components/charts/  PriceChart · Sparkline · ScoreHistoryChart · HalfGauge · SegmentMeter · DivergingBar · ScaleBar
  src/components/stock/   StockPageView · StockHeader · ScoreCard · TrendCard · PriceCard · NewsToneCard · RedFlagsCard
                          WhyCard · BreakdownCard · DriversCard · NewsCard · ScoreHistorySection · FinancialsSection · PeersSection
  src/components/home/    HomeView
  src/components/signals/ SignalsView
  tests/fixtures/{RELIANCE,ABB,HDFCBANK,CHOLAFIN,ADANIENSOL}/*.json · signals-2026-08-14.json · signals-2026-08-15.json · signals-2026-08-16.json · model.json · health.json
  tests/fixtures/index.ts              typed loaders: fixtureStock(symbol) → StockPageData
  tests/unit/*.test.ts(x) · tests/e2e/smoke.spec.ts
```

---

### Task 1: Scaffold, API proxy, typed client, fixtures

**Files:**
- Create: everything under `web/` listed at the top level of the tree, plus `src/lib/api/*`, `scripts/capture-fixtures.mjs`, `tests/fixtures/**`, `tests/unit/client.test.ts`
- Modify: `.claude/launch.json` (add the `web` config)

**Interfaces:**
- Produces:
  - `serverGet<T>(path: string): Promise<T | null>`: base `process.env.SA_API_URL ?? 'http://localhost:8000'` + `/v1` + path, `next: { revalidate: 300 }`.
  - `browserGet<T>(path: string, signal?: AbortSignal): Promise<T | null>`: base `/api/v1`.
  - Both map 404 to `null` and throw `ApiUnavailableError` (exported class, carries `status`) on any other non-2xx or network failure.
  - From `types.ts`, type aliases over `components['schemas']`: `Instrument`, `Signal`, `SignalDetail`, `Explanation`, `Family`, `Driver`, `NewsMix`, `RedFlag`, `Financials`, `Annual`, `Quarter`, `LineItem`, `PriceBar`, `NewsItem`, `ModelInfo`, `Health`.

- [ ] **Step 1: Scaffold the app.**
  - Run `npx create-next-app@latest web`. Answers: TypeScript yes, ESLint yes, Tailwind no, `src/` yes, App Router yes, alias `@/*`, npm.
  - Add `web/.npmrc` with `save-exact=true` and rewrite any `^` ranges in `package.json` to exact versions.
  - `npm i -D vitest @vitejs/plugin-react jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event openapi-typescript @playwright/test`.
  - Scripts: `"test": "vitest run"`, `"typecheck": "tsc --noEmit"`, `"gen:api": "openapi-typescript openapi.json -o src/lib/api/schema.d.ts"`, `"fixtures": "node scripts/capture-fixtures.mjs"`, `"e2e": "playwright test"`.
  - `vitest.config.ts`: environment jsdom, `test.env.TZ = 'Asia/Kolkata'`, setup file importing jest-dom, alias `@` → `src`.
- [ ] **Step 2: Add the proxy.** *(Superseded: see the route handler note under Architecture.)* In `next.config.ts`, `rewrites()` returns `[{ source: '/api/v1/:path*', destination: \`${process.env.SA_API_URL ?? 'http://localhost:8000'}/v1/:path*\` }]`. `.env.example` documents `SA_API_URL=http://localhost:8000`.
- [ ] **Step 3: Generate types.** From the repo root, run `.venv/bin/stockanalysis export-openapi web/openapi.json`, then `npm --prefix web run gen:api`. Write `types.ts` aliases.
- [ ] **Step 4: Capture fixtures.** With the API running, `scripts/capture-fixtures.mjs` fetches into `tests/fixtures/`:
  - Per symbol (RELIANCE, ABB, HDFCBANK, CHOLAFIN, ADANIENSOL): `by-symbol`, `explanation`, `history?limit=500`, `prices?from=2000-01-01`, `financials?quarters=12`, `news?limit=20`, and `peers.json` = `/signals?sector=<sector>&as_of=<explanation.as_of, or model.scored_dates[0] when unscored>&limit=1000`.
  - `/signals?as_of=…&limit=1000` for 2026-08-14, 2026-08-15 and 2026-08-16. Also `/model`, `/health` and `/sectors`.
  - A 404 is written as the JSON literal `null` (HDFCBANK explanation).
  - Run it: `npm --prefix web run fixtures`.
- [ ] **Step 5: Write failing tests** in `tests/unit/client.test.ts`, stubbing `global.fetch`:

```ts
it('maps 404 to null', async () => { stubFetch(404, { detail: 'No signal stored' }); expect(await serverGet('/instruments/X/explanation')).toBeNull(); });
it('throws ApiUnavailableError on 503', async () => { stubFetch(503, {}); await expect(serverGet('/health')).rejects.toMatchObject({ name: 'ApiUnavailableError', status: 503 }); });
it('throws ApiUnavailableError when fetch rejects', async () => { global.fetch = vi.fn().mockRejectedValue(new TypeError('fetch failed')); await expect(serverGet('/health')).rejects.toBeInstanceOf(ApiUnavailableError); });
it('prefixes /v1 and SA_API_URL', async () => { const f = stubFetch(200, []); await serverGet('/sectors'); expect(f.mock.calls[0][0]).toBe('http://localhost:8000/v1/sectors'); });
```

- [ ] **Step 6:** `npm --prefix web test -- client`. Expected: FAIL (module not found).
- [ ] **Step 7:** Implement `client.ts`.
- [ ] **Step 8:** `npm --prefix web test && npm --prefix web run typecheck`. Expected: PASS.
- [ ] **Step 9: Add the launch config.** Add to `.claude/launch.json`: `{ "name": "web", "runtimeExecutable": "npm", "runtimeArgs": ["--prefix", "web", "run", "dev"], "port": 3000 }`. Start it with preview_start `web` (API running). Open `http://localhost:3000/api/v1/health` and check it returns the API's JSON through the proxy.
- [ ] **Step 10:** Commit: `git add web .claude/launch.json && git commit -m "web: scaffold Next.js app, API proxy, typed client, fixtures"`.

### Task 2: Formatting and config modules

**Files:**
- Create: `src/lib/format.ts`, `src/config/{site,signals,redflags,factors}.ts`, `tests/unit/format.test.ts`, `tests/unit/config.test.ts`

**Interfaces:**
- Produces from `format.ts`:
  - `parseDay(iso: string): Date` (local midnight)
  - `fmtDate(iso): string` gives `16 Aug 2026`; `fmtDay(iso): string` gives `16 Aug`
  - `inr(v: number, digits = 2): string`
  - `num(v: number, digits = 0): string`
  - `pct(v: number, digits = 1, signed = false): string` (input is a fraction)
  - `ordinal(n: number): string`
  - `zLabel(z: number): string` gives `+15.2 SD vs sector`
  - `quarterLabel(periodEnd: string): string`
  - `fiscalLabel(fiscalYear: number, periodEnd: string): string`
  - `daysBetween(aIso: string, bIso: string): number`
  - `shortModel(v: string): string` (the text after the last `-`)
- Produces from `site.ts`: `BRAND = 'StockAnalysis'`, `STALE_AFTER_DAYS = 7`, `LOW_COVERAGE = 0.5`, `UNIVERSE = 'NIFTY100'`.
- Produces from `signals.ts`: `type SignalKey = 'BUY' | 'HOLD' | 'SELL'`, `SIGNALS: Record<SignalKey, { label: string; tone: 'pos' | 'hold' | 'neg'; meaning: string }>`, `signalLabel(key: string | null): string` (null gives `Not scored`).
- Produces from `redflags.ts`: `RED_FLAGS: { name: string; description: string; reachable: boolean }[]` (the six in spec §5.1, in that order), `flagStates(red: { name: string; description?: string | null }[] | string[], unknown: string[]): { name: string; label: string; state: 'clear' | 'triggered' | 'unknown' }[]`.
- Produces from `factors.ts`: `FACTOR_UNITS: Record<string, 'percent' | 'pp' | 'multiple' | 'vs_trend' | 'score'>` (the map in spec §5.1), `formatFactorRaw(name: string, raw: number | null): string`.

- [ ] **Step 1: Write the failing tests.** Values come from the RELIANCE and ABB fixtures.

```ts
expect(fmtDate('2026-08-16')).toBe('16 Aug 2026');
expect(num(1075675)).toBe('10,75,675');
expect(inr(1310)).toBe('₹1,310.00');
expect(pct(-0.0464, 2, true)).toBe('−4.64%');           // U+2212 minus everywhere
expect(ordinal(92)).toBe('92nd'); expect(ordinal(21)).toBe('21st'); expect(ordinal(11)).toBe('11th');
expect(zLabel(15.1797)).toBe('+15.2 SD vs sector'); expect(zLabel(-1.3844)).toBe('−1.4 SD vs sector');
expect(quarterLabel('2026-06-30')).toBe("Jun '26");
expect(fiscalLabel(2026, '2026-03-31')).toBe('FY26'); expect(fiscalLabel(2025, '2025-12-31')).toBe('FY25 (Dec)');
expect(formatFactorRaw('book_to_price', 1.0445)).toBe('1.04×');
expect(formatFactorRaw('earnings_yield', 0.0785)).toBe('7.9%');
expect(formatFactorRaw('momentum_12_1', -0.0518)).toBe('−5.2%');
expect(formatFactorRaw('price_to_200dma', 0.9365)).toBe('−6.4% vs trend');
expect(formatFactorRaw('margin_trend', 0.0014)).toBe('+0.14 pp');
expect(formatFactorRaw('mystery', 1.23456)).toBe('1.23');
expect(formatFactorRaw('roe', null)).toBe('—');
expect(signalLabel(null)).toBe('Not scored');
expect(shortModel('phase2-composite-4b05e87d')).toBe('4b05e87d');
const rel = flagStates([], ['promoter_pledge','weak_cash_conversion','contingent_liabilities','rating_downgrade']);
expect(rel.filter(f => f.state === 'clear').map(f => f.name)).toEqual(['auditor_qualification','promoter_selling']);
expect(flagStates([{ name: 'promoter_selling', description: 'Promoter holding falling 3 consecutive quarters' }], []).find(f => f.name === 'promoter_selling')!.state).toBe('triggered');
```

- [ ] **Step 2:** `npm --prefix web test -- format config`. Expected: FAIL.
- [ ] **Step 3: Implement.** Before writing `FACTOR_UNITS`, check each factor's computation in `src/stockanalysis/factors/{value,quality,growth,momentum,sentiment}.py`. If a unit in the spec's map is wrong, follow the code and note the difference in the commit message.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `web: formatters, signal labels, red-flag and factor catalogues`.

### Task 3: Stock view-state derivations

**Files:**
- Create: `src/lib/stock/state.ts`, `src/lib/stock/history.ts`, `src/lib/prices.ts`, `tests/fixtures/index.ts`, `tests/unit/state.test.ts`, `tests/unit/history.test.ts`, `tests/unit/prices.test.ts`

**Interfaces:**
- Consumes: Task 1 types, Task 2 format and config.
- Produces from `state.ts`:
  - `type ScoreState = { kind: 'scored' | 'unscored' | 'below_floor'; score: number | null; signal: SignalKey | null; asOf: string | null; coverage: number | null; lowCoverage: boolean; band: SignalKey | null; overridden: { description: string; wouldBe: SignalKey } | null; stale: { storedVersion: string } | null }`.
  - `scoreState(expl: Explanation | null, model: ModelInfo): ScoreState`. `band` is the band of the score itself. `overridden` is set when the signal is SELL, the band is not SELL, and `red_flags` is non-empty.
  - `whySummary(families: Family[]): string`. Up: contribution > +0.05. Down: contribution < −0.05. Each group is sorted by |contribution| descending. Families are joined "A, B and C". Templates: `"{Up} pull(s) the score up; {down} pull(s) it down"`, `"{Up} pull(s) the score up"`, `"{Down} pull(s) the score down"`, `"No family moves the score much"`.
  - `bandGap(score: number, model: ModelInfo): string`. Below the SELL line: `{ceil(sell − score)} pts to HOLD`. In HOLD: `{ceil(buy − score)} pts to BUY`. At or above BUY: `{floor(score − buy)} pts above the BUY line`.
  - `newsTone(mix: NewsMix | null): { label: 'Positive' | 'Mixed' | 'Negative' | null; needle: number | null; counts: string }`. The needle is `(pos − neg) / total` mapped from [−1, 1] to [0, 1]. Label: needle ≥ 0.6 is Positive, ≤ 0.4 is Negative, otherwise Mixed.
  - `unscoredMessage(sector: string | null): string`
- Produces from `history.ts`:
  - `type Snapshot = { date: string; score: number | null; signal: SignalKey | null; coverage: number | null; model: string; redFlags: string[]; unknownFlags: string[] }`
  - `snapshots(rows: Signal[]): Snapshot[]` (oldest first)
  - `modelChangeIndexes(s: Snapshot[]): number[]` (index i means a change between i−1 and i)
  - `changeSinceFirst(s: Snapshot[]): number | null`
  - `compare(then: Snapshot, now: Snapshot): { scoreDelta: number | null; signalChanged: boolean; comparable: boolean }`
- Produces from `prices.ts`:
  - `type Close = [date: string, close: number]`
  - `toCloses(bars: PriceBar[]): Close[]` (drops null closes, rounds to 2 decimals)
  - `slicePeriod(c: Close[], p: '1M' | '3M' | '6M' | '1Y' | '3Y' | 'MAX'): Close[]` (first bar on or after the same calendar day N months before the last bar)
  - `priceStats(c: Close[], p): { last: Close; change: number; changePct: number; high52: number; low52: number; offHighPct: number } | null` (null for empty input)
- Produces from `tests/fixtures/index.ts`: `fixtureStock(symbol): StockPageData` (type from Task 6, declared here first in `src/lib/stock/load.ts` as a type-only export), `fixtureSignals(date)`, `fixtureModel()`, `fixtureHealth()`.

- [ ] **Step 1: Write the failing tests.**

```ts
const m = fixtureModel();
const rel = scoreState(fixtureStock('RELIANCE').explanation, m);
expect(rel).toMatchObject({ kind: 'scored', signal: 'SELL', band: 'SELL', overridden: null, lowCoverage: false });
expect(bandGap(rel.score!, m)).toBe('17 pts to HOLD');
expect(bandGap(93.29, m)).toBe('18 pts above the BUY line');
expect(scoreState(null, m).kind).toBe('unscored');
const chola = scoreState(fixtureStock('CHOLAFIN').explanation, m);
expect(chola.overridden).toEqual({ description: 'Promoter holding falling 3 consecutive quarters', wouldBe: 'BUY' });
expect(chola.lowCoverage).toBe(true);
expect(chola.stale).toEqual({ storedVersion: expect.stringContaining('a8756fe2') });
// Review Focus 5: a signal row with null score
expect(scoreState({ ...fixtureStock('RELIANCE').explanation!, composite_score: null, signal: null }, m).kind).toBe('below_floor');
expect(whySummary(fixtureStock('RELIANCE').explanation!.families)).toBe('Value pulls the score up; quality and momentum pull it down');
expect(whySummary(fixtureStock('ABB').explanation!.families)).toBe('Quality, momentum and growth pull the score up; value pulls it down');
expect(newsTone(null)).toEqual({ label: null, needle: null, counts: 'No scored news in 30 days' });
expect(newsTone({ positive: 1, negative: 1, neutral: 1, total: 3 })).toMatchObject({ label: 'Mixed', needle: 0.5, counts: '1 positive · 1 negative · 1 neutral' });
expect(unscoredMessage('Financial Services')).toContain('banks and NBFCs');

const s = snapshots(fixtureStock('RELIANCE').history);
expect(s.map(x => x.date)).toEqual(['2026-07-31', '2026-08-14', '2026-08-16']);
expect(modelChangeIndexes(s)).toEqual([2]);
expect(changeSinceFirst(s)!.toFixed(1)).toBe('-4.1');
expect(compare(s[0], s[2]).comparable).toBe(false);
expect(bandGap(60, m)).toBe('15 pts to BUY');
expect(changeSinceFirst(snapshots(fixtureStock('ADANIENSOL').history))).toBeNull();

const c = toCloses(fixtureStock('RELIANCE').prices);
expect(slicePeriod(c, '1Y')[0]).toEqual(['2025-08-14', 1373.8]);          // TZ=Asia/Kolkata in vitest config
const st = priceStats(c, '1Y')!;
expect(st.change.toFixed(2)).toBe('-63.80'); expect((st.changePct * 100).toFixed(2)).toBe('-4.64');
expect(st.high52).toBe(1592.3); expect(st.low52).toBe(1258.8);
expect(priceStats([], '1Y')).toBeNull();                                    // Review Focus 3
```

- [ ] **Step 2:** `npm --prefix web test -- state history prices`. Expected: FAIL.
- [ ] **Step 3:** Implement the three modules and `tests/fixtures/index.ts`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `web: score state, history and price derivations`.

### Task 4: Tokens, UI primitives, app shell and search

**Files:**
- Create: `src/app/globals.css`, `src/app/layout.tsx`, `src/components/ui/*`, `src/components/shell/*`, `tests/unit/search.test.tsx`, `tests/unit/freshness.test.tsx`

**Interfaces:**
- Consumes: `browserGet`, `serverGet`, `Health`, `Instrument`, `fmtDate`, `daysBetween`, `STALE_AFTER_DAYS`, `BRAND`.
- Produces:
  - `<AppShell freshness={{ scores: string | null; prices?: string | null; filings?: string | null }} crumbs={{ label: string; href?: string }[]}>`
  - `<SearchBox autoFocus?: boolean size?: 'hero' | 'bar' />`
  - `<SignalChip signal: SignalKey | null />`
  - `<Card>`, `<Pill tone>`
  - `<Segmented options value onChange>` (client)
  - `<InfoTip text>`
  - `<Disclaimer />`
  - The root `layout.tsx` fetches `/health` once for the freshness card.

- [ ] **Step 1: Write the failing tests.** Use user-event and a stubbed `browserGet`.

```tsx
it('does not query for whitespace', async () => { render(<SearchBox />); await user.type(input(), '   '); await advance(200); expect(browserGet).not.toHaveBeenCalled(); });
it('debounces ~150ms and lists symbol, name, sector', async () => { stubSearch('reli', [RELIANCE]); await user.type(input(), 'reli'); await advance(160); expect(await screen.findByRole('option', { name: /RELIANCE.*Reliance Industries Ltd\./ })).toBeVisible(); });
it('ignores a stale response that resolves after a newer one', async () => { /* 'rel' resolves after 'reli' with [RELAXO]; expect only RELIANCE options */ });
it('ArrowDown + Enter navigates to /stocks/RELIANCE', async () => { /* expect router.push('/stocks/RELIANCE') */ });
it('Escape closes the list', async () => { /* listbox disappears */ });
it('freshness dot is amber when older than 7 days', () => { render(<FreshnessCard today="2026-10-24" scores="2026-08-16" />); expect(screen.getByText('16 Aug 2026').closest('[data-stale]')).toHaveAttribute('data-stale', 'true'); });
```

- [ ] **Step 2:** `npm --prefix web test -- search freshness`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Tokens and base styles go in `globals.css`. Copy the values from the mock's `:root`.
  - Use Inter via `next/font/google`.
  - Shell layout: sidebar 248px, with the items and "soon" markers from spec §5.
  - The phone shell (< 720px) has a top bar and a drawer (`<dialog>` or an aria-modal panel).
  - ⌘K / Ctrl-K focuses the bar search.
  - SearchBox uses `role="combobox"` and `listbox`/`option`, an `AbortController` per query, and a request id so stale responses are ignored.
- [ ] **Step 4:** Run the tests. Expected: PASS. Then run `npm --prefix web run typecheck`.
- [ ] **Step 5:** Commit: `web: tokens, primitives, app shell, search with autocomplete`.

### Task 5: Chart primitives (SVG)

Load the `dataviz` skill before writing chart code. It covers mark specs, recessive grid and the hover layer.

**Files:**
- Create: `src/components/charts/*.tsx` (+ css), `tests/unit/charts.test.tsx`

**Interfaces:**
- Consumes: `Close`, `Snapshot`, `modelChangeIndexes`, `fmtDate`, `inr`.
- Produces:
  - `<PriceChart closes: Close[] />` (client). Right axis with nice steps, 3–4 date ticks, crosshair, dot and tooltip "₹1,383.00 · 24 Sept 2025". Resizes with its container.
  - `<Sparkline points: Snapshot[] />` (score trend with model-change markers)
  - `<ScoreHistoryChart points: Snapshot[] thresholds: { sell: number; buy: number } selected?: string />`
  - `<HalfGauge value: number | null />`. Null renders no needle.
  - `<SegmentMeter score: number | null thresholds />`. 20 segments, ▼ marker. Null renders all segments grey and no marker.
  - `<DivergingBar value: number | null max: number />`. Null renders hatched.
  - `<ScaleBar score: number | null thresholds label: string />`
  - Every chart has `role="img"` and an `aria-label` summary.

- [ ] **Step 1: Write the failing tests.**

```tsx
it('SegmentMeter places the marker at the score', () => { render(<SegmentMeter score={28.4} thresholds={{ sell: 45, buy: 75 }} />); expect(screen.getByTestId('meter-marker')).toHaveStyle({ left: '28.4%' }); });
it('SegmentMeter with null score has no marker', () => { render(<SegmentMeter score={null} thresholds={{ sell: 45, buy: 75 }} />); expect(screen.queryByTestId('meter-marker')).toBeNull(); });
it('DivergingBar null renders hatched, not NaN', () => { const { container } = render(<DivergingBar value={null} max={0.45} />); expect(container.querySelector('[data-hatched]')).not.toBeNull(); expect(container.innerHTML).not.toMatch(/NaN/); });
it('DivergingBar negative grows left of centre', () => { render(<DivergingBar value={-0.178} max={0.45} />); expect(screen.getByTestId('bar')).toHaveStyle({ right: '50%' }); });
it('Sparkline draws one dashed marker per model change', () => { const { container } = render(<Sparkline points={snapshots(fixtureStock('RELIANCE').history)} />); expect(container.querySelectorAll('[data-model-change]')).toHaveLength(1); });
it('PriceChart has an accessible summary', () => { render(<PriceChart closes={slicePeriod(toCloses(fixtureStock('RELIANCE').prices), '1Y')} />); expect(screen.getByRole('img', { name: /RELIANCE|price/i })).toBeInTheDocument(); });
```

- [ ] **Step 2:** `npm --prefix web test -- charts`. Expected: FAIL.
- [ ] **Step 3:** Implement. Port the geometry from the mock's `drawChart`, sparkline and gauge functions.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `web: SVG chart primitives`.

### Task 6: Stock page, top half (loader, header, overview grid, states)

**Files:**
- Modify: `src/lib/stock/load.ts` (the type exists from Task 3; add the loader)
- Create: `src/app/stocks/[symbol]/{page,not-found,error}.tsx`, `src/components/stock/{StockPageView,StockHeader,ScoreCard,TrendCard,PriceCard,NewsToneCard,RedFlagsCard,WhyCard}.tsx` (+ css), `tests/unit/load.test.ts`, `tests/unit/stock-top.test.tsx`

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces:
  - `type StockPageData = { instrument: Instrument; explanation: Explanation | null; history: Signal[]; prices: PriceBar[]; financials: Financials; news: NewsItem[]; model: ModelInfo; peers: Signal[]; peersAsOf: string | null }`
  - `loadStockPage(symbol: string, get = serverGet): Promise<StockPageData | null>`. It resolves `/instruments/by-symbol/{encodeURIComponent(symbol.toUpperCase())}` and returns `null` on 404. It then fetches the other routes in parallel. Peers: `/signals?sector=…&as_of={explanation.as_of ?? model.scored_dates[0]}&limit=1000`.
  - `<StockPageView data={StockPageData} today?: string />` (synchronous; used by the page and by tests)
  - Section anchors: `#overview`, `#breakdown`, `#history`, `#financials`, `#news`, `#peers`. Task 7 renders these sections inside `StockPageView`.
  - The canonical URL redirect: lower-case `symbol` → `redirect('/stocks/RELIANCE')`.

- [ ] **Step 1: Write the failing tests.**

```ts
// load.test.ts: get is a stub recording paths
await loadStockPage('m&m', get);       expect(get).toHaveBeenCalledWith('/instruments/by-symbol/M%26M');           // Review Focus 1
await loadStockPage('bajaj-auto', get); expect(get).toHaveBeenCalledWith('/instruments/by-symbol/BAJAJ-AUTO');
expect(await loadStockPage('NOPE', getReturningNull)).toBeNull();
await expect(loadStockPage('RELIANCE', getThrowing503)).rejects.toBeInstanceOf(ApiUnavailableError);           // Review Focus 2
```

```tsx
// stock-top.test.tsx
renderView('RELIANCE'):
  expect(screen.getByTestId('score-value')).toHaveTextContent('28');
  expect(screen.getAllByText('SELL').length).toBeGreaterThan(0);
  expect(screen.getByText('Scored 16 Aug 2026 · model 4b05e87d')).toBeInTheDocument();
  expect(screen.getByText('17 pts to HOLD')).toBeInTheDocument();
  expect(screen.getByText('90% of factors measured')).toBeInTheDocument();
  expect(screen.getByText('Value pulls the score up; quality and momentum pull it down')).toBeInTheDocument();
  expect(screen.getByText('NSE close · 14 Aug 2026')).toBeInTheDocument();
  expect(screen.getByText('Unchecked ≠ clean')).toBeInTheDocument();
renderView('HDFCBANK'):
  expect(screen.getByText('Not scored')).toBeInTheDocument();
  expect(screen.queryByText(/\/ 100/)).toBeNull();                       // never a 0 / 100
  expect(screen.getByText(/banks and NBFCs/)).toBeInTheDocument();
  expect(screen.queryByTestId('why-card')).toBeNull(); expect(screen.queryByTestId('trend-card')).toBeNull();
renderView('CHOLAFIN'):
  expect(screen.getByRole('alert')).toHaveTextContent('SELL forced by red flag: Promoter holding falling 3 consecutive quarters. Without it, the score would read BUY.');
  expect(screen.getByText('Based on 23% of factors, treat with caution')).toBeInTheDocument();
renderView('ABB'):  expect(screen.getByText('No scored news in 30 days')).toBeInTheDocument();
renderView('ADANIENSOL'): expect(screen.getByText('First scored 16 Aug 2026')).toBeInTheDocument();
renderView with prices: []: expect(screen.getByText('No price data stored')).toBeInTheDocument();   // Review Focus 3
renderView('RELIANCE' with explanation.composite_score = null, signal = null):
  expect(screen.getByText('Not scored on 16 Aug 2026: only 90% of factors measured.')).toBeInTheDocument();   // Review Focus 5
  expect(document.body.innerHTML).not.toMatch(/NaN/);
renderView('RELIANCE' with explanation.narrative = 'Test narrative.'): expect(within(screen.getByTestId('why-card')).getByText('Test narrative.')).toBeInTheDocument();
```

- [ ] **Step 2:** `npm --prefix web test -- load stock-top`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Lay out the overview grid with `grid-template-areas` and container queries at the global breakpoints, following the mock's CSS.
  - Phone order: score → why → price → trend → checks.
  - `error.tsx` shows "Data is temporarily unavailable" and a retry button calling `reset()`.
  - `not-found.tsx` shows "No NIFTY100 company with symbol {symbol}" and a `<SearchBox />`.
  - Set `generateMetadata` title to `{SYMBOL} · {name} | {BRAND}`.
  - Pass the freshness card this company's dates: scores = `explanation.as_of`, prices = last bar date, filings = newest `filing_date` across quarterly and annual.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Check in the browser.** With the API and web running, open `/stocks/RELIANCE`, `/stocks/reliance` (redirects), `/stocks/HDFCBANK`, `/stocks/CHOLAFIN` and `/stocks/NOPE` at 1440×900 and 390×844 using the browser pane. Compare with the mock and check for console errors.
- [ ] **Step 6:** Commit: `web: stock page loader, header and overview cards`.

### Task 7: Stock page, lower sections

**Files:**
- Create: `src/components/stock/{BreakdownCard,DriversCard,NewsCard,ScoreHistorySection,FinancialsSection,PeersSection}.tsx` (+ css), `tests/unit/stock-lower.test.tsx`
- Modify: `src/components/stock/StockPageView.tsx` (render the sections in spec order)

**Interfaces:**
- Consumes: `StockPageData`, `snapshots`, `compare`, `formatFactorRaw`, `zLabel`, `fiscalLabel`, `quarterLabel`, `ScoreHistoryChart`, `DivergingBar`, `Segmented`.
- Produces: no new exports beyond the components.

- [ ] **Step 1: Write the failing tests.**

```tsx
renderView('RELIANCE'):
  // breakdown
  expect(within(breakdown()).getByText('92nd')).toBeInTheDocument(); expect(within(breakdown()).getByText('n/m')).toBeInTheDocument();
  expect(within(breakdown()).getByText(/Sentiment wasn't measured/)).toBeInTheDocument();
  // drivers
  expect(screen.getByText('+15.2 SD vs sector')).toBeInTheDocument(); expect(screen.getByText('1.04×')).toBeInTheDocument();
  expect(screen.getByText('Positive = cheap on book value')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Pulling down' })); expect(screen.getByText('−1.4 SD vs sector')).toBeInTheDocument();
  // news
  expect(screen.getAllByText('Not scored yet').length).toBeGreaterThan(0);   // after switching to "All 19"
  // history
  await user.selectOptions(screen.getByLabelText('Compare with'), '2026-07-31');
  expect(screen.getByText(/not strictly comparable/)).toBeInTheDocument();
  // financials
  expect(within(financials()).getByText("Jun '26")).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Annual' }));
  expect(within(financials()).getByText('10,75,675')).toBeInTheDocument(); expect(within(financials()).getByText('FY26')).toBeInTheDocument();
  expect(within(rowNamed('EBITDA')).getAllByTitle('Not in the filing').length).toBeGreaterThan(0);
  // peers (16 Aug: no other Oil Gas company scored)
  expect(screen.getByText('No other Oil Gas & Consumable Fuels company was scored on 16 Aug 2026')).toBeInTheDocument();
renderView('ABB'):      (Annual) expect(screen.getByText('FY25 (Dec)')).toBeInTheDocument();
renderView('HDFCBANK'): (Annual) expect(screen.getByText('No annual figures in this company\'s filings')).toBeInTheDocument();
renderView('CHOLAFIN'): expect(screen.getByText(/re-derived with current weights/)).toBeInTheDocument();
```

- [ ] **Step 2:** `npm --prefix web test -- stock-lower`. Expected: FAIL.
- [ ] **Step 3:** Implement per spec §5.1:
  - Score history: chart, then "Compare with" select, then the then-vs-now panel, then a table newest first, then a "Family changes: coming soon" row.
  - Section tabs become anchor links with a scroll-spy underline.
  - Financial tables scroll horizontally on a phone with a sticky first column.
- [ ] **Step 4:** Run the tests. Expected: PASS. Repeat the browser check from Task 6 Step 5, including scrolling through every section at 390 px.
- [ ] **Step 5:** Commit: `web: stock page breakdown, drivers, news, score history, financials, peers`.

### Task 8: Signals list and home

**Files:**
- Create: `src/lib/signals/{list,load}.ts`, `src/app/signals/page.tsx`, `src/components/signals/SignalsView.tsx`, `src/app/page.tsx`, `src/components/home/HomeView.tsx` (+ css), `tests/unit/signals.test.ts`, `tests/unit/signals-view.test.tsx`, `tests/unit/home.test.tsx`

**Interfaces:**
- Consumes: `serverGet`, `Signal`, `ModelInfo`, `Health`, `SIGNALS`, `SignalChip`, `SearchBox`, `Segmented`.
- Produces from `list.ts`:
  - `type Row = Signal & { delta: number | null; modelChanged: boolean }`
  - `withPrevious(current: Signal[], previous: Signal[]): Row[]`
  - `sortRows(rows: Row[], key: 'score' | 'delta' | 'coverage' | 'symbol', dir: 'asc' | 'desc'): Row[]` (null scores always last)
  - `partialNotice(count: number, instruments: number, fuller: { date: string; count: number } | null): string | null` (a notice when count < 0.5 × instruments)
  - `counts(rows: Signal[]): Record<SignalKey, number>`
- Produces from `load.ts`:
  - `loadSignals(q: { signal?: string; sector?: string; as_of?: string }, get = serverGet): Promise<{ rows: Row[]; asOf: string; dates: string[]; sectors: string[]; notice: string | null }>`. The previous date is the next older entry in `model.scored_dates`. The fuller date is found by walking older `scored_dates`, at most 5 lookups, until count ≥ 0.5 × `health.instruments`.
  - `loadHome(get = serverGet): Promise<{ asOf: string; counts; notScored: number; top: Signal[]; bottom: Signal[]; flagged: Signal[]; model: ModelInfo; notice: string | null }>`

- [ ] **Step 1: Write the failing tests.**

```ts
const rows = withPrevious(fixtureSignals('2026-08-16'), fixtureSignals('2026-08-15'));
expect(rows.find(r => r.nse_symbol === 'ABB')!.delta!.toFixed(1)).toBe('43.3');
expect(rows.find(r => r.nse_symbol === 'RELIANCE')!.delta).toBeNull();
expect(sortRows([...rows, { ...rows[0], nse_symbol: 'ZZZ', composite_score: null } as Row], 'score', 'asc').at(-1)!.nse_symbol).toBe('ZZZ');
expect(partialNotice(4, 100, { date: '2026-08-14', count: 77 })).toBe('Only 4 of 100 companies were scored on 16 Aug 2026. The most recent fuller list is 14 Aug 2026 (77 companies).');
expect(partialNotice(77, 100, null)).toBeNull();
expect(counts(fixtureSignals('2026-08-14'))).toEqual({ BUY: 16, HOLD: 19, SELL: 42 });   // all 77 rows have a signal
```

```tsx
// signals-view: renders "{N} stocks scored on 16 Aug 2026", the notice with a link to ?as_of=2026-08-14,
// a row per stock linking to /stocks/{symbol}, coverage < 50% marked amber (data-low-coverage), and a card list at 390px (container query class present).
// home: hero SearchBox present; "BUY 1 · HOLD 0 · SELL 3" for 16 Aug fixture; top list starts with ABB; red-flag list contains ADANIENSOL; disclaimer present.
```

- [ ] **Step 2:** `npm --prefix web test -- signals home`. Expected: FAIL.
- [ ] **Step 3:** Implement per spec §5.2–5.3. Filters live in `searchParams`. The Segmented and select controls update the URL with `router.replace`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Browser check of `/` and `/signals?as_of=2026-08-14&signal=BUY` at both widths.
- [ ] **Step 5:** Commit: `web: signals list and home`.

### Task 9: Secondary pages, smoke suite, final verification

**Files:**
- Create: `src/app/{learn,disclaimer,red-flags}/page.tsx`, `src/app/not-found.tsx`, `playwright.config.ts`, `tests/e2e/smoke.spec.ts`

**Interfaces:**
- Consumes: `serverGet('/model')`, `serverGet('/signals/red-flags')`, `Disclaimer`, `AppShell`.

- [ ] **Step 1: Build the secondary pages.**
  - `learn`: families and weights from `/v1/model`, thresholds, and the sentence "Scores are percentiles within the scored universe on each date, not valuations."
  - `disclaimer`: long-form placeholder plus the §6.6 text.
  - `red-flags`: the signals table filtered to flagged names via `/v1/signals/red-flags`, plus the six-flag catalogue with "no data source" on the unreachable two.
  - The sidebar's **Sectors** link goes to `/signals` with the sector select focused. There's no separate page in v1.
- [ ] **Step 2: Write the smoke suite.**
  - `playwright.config.ts`: `webServer` runs `npm run dev`, expects the API already on :8000, and defines projects `desktop` (1440×900) and `phone` (390×844, `isMobile`).
  - `smoke.spec.ts` visits `/`, `/stocks/RELIANCE`, `/stocks/HDFCBANK`, `/stocks/CHOLAFIN`, `/signals`, `/learn`, `/stocks/NOPE`. It asserts no console errors and no horizontal page scroll (`document.documentElement.scrollWidth <= innerWidth`). `NOPE` must render the not-found text.
  - Running it needs `npx playwright install chromium`, a one-time browser download. **Ask the user before downloading.** If they decline, do the same checks by hand in the browser pane and record the results in the commit message.
- [ ] **Step 3:** `npm --prefix web test && npm --prefix web run typecheck && npm --prefix web run lint && npm --prefix web run build`. Expected: all pass. The build must not need the API, because pages are dynamic. If `next build` tries to prerender a data route, mark it `export const dynamic = 'force-dynamic'` or keep `revalidate`, and fetch at request time.
- [ ] **Step 4:** `npm --prefix web run e2e`. Expected: all pass on both projects.
- [ ] **Step 5:** Commit: `web: learn, disclaimer, red-flags pages and smoke suite`.
