import { afterEach, describe, expect, it, vi } from "vitest";

import { accountsEnabled, safeNext } from "@/lib/auth/config";

describe("safeNext", () => {
  it.each([
    ["/portfolio", "/portfolio"],
    ["/stocks/M%26M?x=1", "/stocks/M%26M?x=1"],
    ["//evil.test", "/portfolio"],
    ["/\\evil.test", "/portfolio"],
    ["https://evil.test", "/portfolio"],
    [undefined, "/portfolio"],
    ["", "/portfolio"],
  ])("safeNext(%s) is %s", (raw, out) => expect(safeNext(raw)).toBe(out));

  it.each([
    // Browsers drop tabs and newlines from URLs, which would leave "//evil.test".
    "/\t/evil.test",
    "/\n/evil.test",
    // Dot segments that collapse into a protocol-relative path.
    "/.//evil.test",
    "evil.test",
    "javascript:alert(1)",
  ])("safeNext(%j) stays on the site", (raw) => expect(safeNext(raw)).toBe("/portfolio"));

  it("treats null like a missing value", () => expect(safeNext(null)).toBe("/portfolio"));
});

describe("accountsEnabled", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("accounts are enabled only with a database and a secret", () => {
    const cases: [string | undefined, string | undefined, boolean][] = [
      ["postgresql:///stockanalysis", "s3cret", true],
      ["postgresql:///stockanalysis", undefined, false],
      ["postgresql:///stockanalysis", "", false],
      [undefined, "s3cret", false],
      ["", "s3cret", false],
      [undefined, undefined, false],
    ];
    for (const [database, secret, enabled] of cases) {
      vi.stubEnv("DATABASE_URL", database);
      vi.stubEnv("BETTER_AUTH_SECRET", secret);
      expect(accountsEnabled(), `DATABASE_URL=${database} BETTER_AUTH_SECRET=${secret}`).toBe(enabled);
    }
  });
});
