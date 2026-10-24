import { expect, test, type Page } from "@playwright/test";

// Texts that hold whatever date the pipeline last scored, so a re-run
// doesn't break the smoke suite.
const PAGES: { path: string; text: string | RegExp; status?: number }[] = [
  { path: "/", text: "Search NIFTY100 stocks" },
  { path: "/stocks/RELIANCE", text: /^Scored \d{1,2} [A-Z][a-z]{2} \d{4}/ },
  { path: "/stocks/HDFCBANK", text: "Not scored" },
  { path: "/stocks/CHOLAFIN", text: /SELL forced by red flag/ },
  { path: "/signals", text: /stocks scored on/ },
  { path: "/learn", text: "Scores are normal-curve percentiles relative to the companies scored that day, not valuations." },
  { path: "/red-flags", text: "The model's six checks" },
  { path: "/disclaimer", text: "Placeholder text, pending legal review. The final wording will replace this page." },
  { path: "/stocks/NOPE", text: "No NIFTY100 company with symbol NOPE", status: 404 },
];

function collectErrors(page: Page, expectedStatus?: number): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // The browser logs the document's own 404 status on the not-found page.
    if (expectedStatus === 404 && /status of 404/.test(m.text())) return;
    errors.push(`console: ${m.text()}`);
  });
  return errors;
}

for (const p of PAGES) {
  test(`${p.path} renders without errors or sideways scroll`, async ({ page }) => {
    const errors = collectErrors(page, p.status);
    const res = await page.goto(p.path);
    expect(res?.status()).toBe(p.status ?? 200);
    // Visible text only: the search box also carries a screen-reader label.
    await expect(page.getByText(p.text).locator("visible=true").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    expect(errors).toEqual([]);
  });
}

test("lower-case symbol redirects to the canonical URL", async ({ page }) => {
  await page.goto("/stocks/reliance");
  await expect(page).toHaveURL(/\/stocks\/RELIANCE$/);
});

test("search finds a stock and opens its page", async ({ page }) => {
  await page.goto("/");
  const box = page.getByRole("main").getByRole("combobox").last();
  await box.fill("reli");
  await page.getByRole("option", { name: /RELIANCE/ }).click();
  await expect(page).toHaveURL(/\/stocks\/RELIANCE$/);
});
