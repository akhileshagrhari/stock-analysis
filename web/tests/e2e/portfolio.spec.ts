import { expect, test, type Page } from "@playwright/test";

// Needs accounts switched on (DATABASE_URL and BETTER_AUTH_SECRET in .env.local),
// AUTH_DEV_FIXED_OTP, which playwright.config.ts sets, and the API on :8000
// serving Postgres with the SA_AUTH_* settings.
const CODE = "246810";
// Relative to web/, where `npm run e2e` runs. Made-up numbers, never the owner's file.
const FIXTURE = "../tests/fixtures/portfolio/kite_holdings.csv";
const EMPTY = "See your Zerodha holdings beside the model's latest rating of each stock.";

// Accounts a test made and hasn't deleted: the sign-out test's, or one from a
// test that failed partway. afterEach removes them, so a run leaves no test
// user or portfolio behind (deleting an account deletes its portfolio first).
const leftover = new Set<string>();

function newEmail(project: string): string {
  return `e2e-${project}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.test`;
}

async function signIn(page: Page, email: string, next: string): Promise<void> {
  leftover.add(email);
  await page.goto(`/login?next=${next}`);
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Send code" }).click();
  await page.getByLabel("Code").fill(CODE);
  await page.getByRole("button", { name: "Sign in" }).click();
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text()}`);
  });
  return errors;
}

/** Accept the next confirm() and return its message. */
function acceptNextDialog(page: Page): Promise<string> {
  return new Promise((resolve) => {
    page.once("dialog", async (d) => {
      const message = d.message();
      await d.accept();
      resolve(message);
    });
  });
}

async function importFile(page: Page, mode: "Replace my portfolio" | "Add to my portfolio"): Promise<void> {
  await page.getByRole("link", { name: "Import from Zerodha" }).click();
  await expect(page).toHaveURL(/\/portfolio\/import$/);
  await page.getByRole("button", { name: mode }).click();
  await expect(page.getByRole("button", { name: mode })).toHaveAttribute("aria-pressed", "true");
  await page.locator('input[type="file"]').setInputFiles(FIXTURE);
  await page.getByRole("button", { name: "Save portfolio" }).click();
  await expect(page).toHaveURL(/\/portfolio\?saved=\d+$/);
}

test.afterEach(async ({ page, baseURL }) => {
  // Sign in again through the API with the fixed code, then delete the
  // account as the account menu does: one request, which deletes the
  // portfolio first on the server.
  const headers = { origin: baseURL! };
  for (const email of leftover) {
    const sent = await page.request.post("/api/auth/email-otp/send-verification-otp", { headers, data: { email, type: "sign-in" } });
    expect(sent.ok(), `cleanup: send a code to ${email}`).toBe(true);
    const signedIn = await page.request.post("/api/auth/sign-in/email-otp", { headers, data: { email, otp: CODE } });
    expect(signedIn.ok(), `cleanup: sign in as ${email}`).toBe(true);
    expect((await page.request.post("/api/auth/delete-user", { headers, data: {} })).ok(), `cleanup: delete ${email}`).toBe(true);
    leftover.delete(email);
  }
});

test("signs in with an emailed code, then signs out", async ({ page }, testInfo) => {
  await signIn(page, newEmail(testInfo.project.name), "/");

  await expect(page).toHaveURL(/\/$/);
  // Sign-in ends with router.push then router.refresh. The session (and so the
  // avatar) can be ready before the refresh lands, and the re-render closes a
  // menu opened in between, so let the page settle first.
  await page.waitForLoadState("networkidle");
  const avatar = page.getByRole("button", { name: "Account" });
  await expect(avatar).toBeVisible();

  await avatar.click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
});

test("imports a Kite file, groups it by rating, adds to it, then deletes it and the account", async ({ page }, testInfo) => {
  // Two imports and two deletes, each a first compile on a cold dev server.
  test.slow();
  const errors = collectErrors(page);
  const email = newEmail(testInfo.project.name);
  await signIn(page, email, "/portfolio");

  await expect(page).toHaveURL(/\/portfolio$/);
  await expect(page.getByText(EMPTY)).toBeVisible();

  await importFile(page, "Replace my portfolio");
  await expect(page.getByText("Saved 23 holdings from kite_holdings.csv (replaced your portfolio)", { exact: true })).toBeVisible();

  const notRated = page.locator('tbody[data-group="NOT_RATED"]');
  await expect(notRated.getByText("Not rated", { exact: true })).toBeVisible();
  await expect(notRated.getByText("NIFTYBEES", { exact: true })).toBeVisible();
  const rated = page.locator('tbody[data-group="BUY"], tbody[data-group="HOLD"], tbody[data-group="SELL"]');
  await expect(rated.locator("th").getByText(/^(BUY|HOLD|SELL)$/).first()).toBeVisible();

  await page.waitForLoadState("networkidle");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);

  await importFile(page, "Add to my portfolio");
  await expect(page.getByText("Saved 23 holdings from kite_holdings.csv (added to your portfolio)", { exact: true })).toBeVisible();

  const portfolioAsked = acceptNextDialog(page);
  await page.getByRole("button", { name: "Delete portfolio" }).click();
  expect(await portfolioAsked).toBe("Delete your saved portfolio? This can't be undone.");
  await expect(page.getByText(EMPTY)).toBeVisible();

  await page.getByRole("button", { name: "Account" }).click();
  const accountAsked = acceptNextDialog(page);
  await page.getByRole("button", { name: "Delete account" }).click();
  expect(await accountAsked).toBe("Delete your account and portfolio? This can't be undone.");
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  leftover.delete(email);

  expect(errors).toEqual([]);
});
