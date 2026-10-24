# StockAnalysis public site

Next.js app that renders the read-only FastAPI `/v1` API: home with search, a
stock page per NSE symbol, and the signals list.

- Design: `docs/superpowers/specs/2026-10-24-public-site-ui-design.md`
- Plan: `docs/superpowers/plans/2026-10-24-public-site-ui.md`
- API gaps for the backend: `docs/superpowers/specs/2026-10-24-site-api-requirements.md`

## Run

The API must be running first (`.venv/bin/stockanalysis serve-api`, port 8000).

```bash
npm install
npm run dev          # http://localhost:3000
```

`SA_API_URL` (default `http://localhost:8000`) is where the API is. Server
components call it directly; the browser goes through the `/api/v1/*` rewrite in
`next.config.ts`, so the API needs no CORS settings.

## Check

```bash
npm test             # unit tests (Vitest, TZ=Asia/Kolkata) against captured fixtures
npm run typecheck
npm run lint
npm run build        # needs no API: every data page renders on request
npm run e2e          # Playwright smoke at 1440x900 and 390x844; needs the API
```

`npm run e2e` uses Playwright's own Chromium (`npx playwright install chromium`).
To use an installed Google Chrome instead: `PLAYWRIGHT_CHANNEL=chrome npm run e2e`.

## Keep in sync with the API

- `npm run gen:api` after `stockanalysis export-openapi web/openapi.json` (from the repo root) regenerates `src/lib/api/schema.d.ts`.
- `npm run fixtures` re-captures the test fixtures from a running API. Tests pin values from today's data (for example RELIANCE 28 on 16 Aug 2026), so expect to update assertions after a pipeline run.

## Where things live

- `src/config/`: signal labels (the only place BUY / HOLD / SELL are written), brand, red-flag and factor catalogues.
- `src/lib/`: formatting and all view logic as plain functions (tested).
- `src/components/`: shell, charts (hand-rolled SVG), stock page, signals, home, learn pages.
