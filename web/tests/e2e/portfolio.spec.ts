import { expect, test } from "@playwright/test";

// Needs accounts switched on (DATABASE_URL and BETTER_AUTH_SECRET in .env.local)
// and AUTH_DEV_FIXED_OTP, which playwright.config.ts sets.
test("signs in with an emailed code, then signs out", async ({ page }) => {
  await page.goto("/login?next=/");
  await page.getByLabel("Email").fill(`e2e-${Date.now()}@example.test`);
  await page.getByRole("button", { name: "Send code" }).click();
  await page.getByLabel("Code").fill("246810");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/$/);
  const avatar = page.getByRole("button", { name: "Account" });
  await expect(avatar).toBeVisible();

  await avatar.click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
});
