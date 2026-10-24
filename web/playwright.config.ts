import { defineConfig, devices } from "@playwright/test";

// Smoke tests against the dev server and a running local API on :8000
// (`.venv/bin/stockanalysis serve-api`). PLAYWRIGHT_CHANNEL=chrome uses the
// installed Google Chrome instead of Playwright's own Chromium download.
const channel = process.env.PLAYWRIGHT_CHANNEL || undefined;

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 60_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:3000", channel },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000/api/v1/instruments/search?q=a&limit=1",
    reuseExistingServer: true,
    timeout: 120_000,
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], channel, viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { ...devices["Pixel 7"], channel, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
