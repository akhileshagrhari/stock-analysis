import { defineConfig, devices } from "@playwright/test";

// Smoke tests against this checkout's dev server and a running local API on
// :8000 (`.venv/bin/stockanalysis serve-api`).
//
// - PLAYWRIGHT_PORT (default 3000): the port the dev server is started on.
// - PLAYWRIGHT_REUSE=1: test a dev server already running on that port. Only
//   do this when you started it from this checkout; otherwise a busy port
//   fails the run rather than testing whatever else is listening there.
// - PLAYWRIGHT_CHANNEL=chrome: use the installed Google Chrome instead of
//   Playwright's own Chromium download.
const port = process.env.PLAYWRIGHT_PORT || "3000";
const baseURL = `http://localhost:${port}`;
const channel = process.env.PLAYWRIGHT_CHANNEL || undefined;

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 60_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: { baseURL, channel },
  webServer: {
    command: `npm run dev -- --port ${port}`,
    url: `${baseURL}/api/v1/instruments/search?q=a&limit=1`,
    reuseExistingServer: process.env.PLAYWRIGHT_REUSE === "1",
    timeout: 120_000,
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], channel, viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { ...devices["Pixel 7"], channel, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
