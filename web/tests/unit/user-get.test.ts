// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ apiToken: vi.fn(), fetch: vi.fn() }));
vi.mock("@/lib/auth/server", () => ({ apiToken: mocks.apiToken }));
vi.mock("next/headers", () => ({ headers: async () => new Headers({ cookie: "s=1" }) }));

import { ApiUnavailableError } from "@/lib/api/client";
import { SignedOutError, userGet } from "@/lib/api/user";

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.apiToken.mockResolvedValue("tok");
});

describe("userGet", () => {
  it("returns the body, sending the token, no-store and a timeout", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ a: 1 }));
    expect(await userGet("/me/portfolio")).toEqual({ a: 1 });
    const init = mocks.fetch.mock.calls[0][1];
    expect(init.cache).toBe("no-store");
    expect(init.headers.authorization).toBe("Bearer tok");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("throws SignedOutError with no token, without calling the API", async () => {
    mocks.apiToken.mockResolvedValue(null);
    await expect(userGet("/me/portfolio")).rejects.toBeInstanceOf(SignedOutError);
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("throws SignedOutError on a 401", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ detail: "Sign in again." }, { status: 401 }));
    await expect(userGet("/me/portfolio")).rejects.toBeInstanceOf(SignedOutError);
  });

  it("is null for the API's own 404", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ detail: "No portfolio." }, { status: 404 }));
    expect(await userGet("/me/portfolio")).toBeNull();
  });

  it("is an outage for FastAPI's bare Not Found", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ detail: "Not Found" }, { status: 404 }));
    await expect(userGet("/me/portfolio")).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("is an outage for a 5xx or an unreachable API", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ detail: "x" }, { status: 503 }));
    await expect(userGet("/me/portfolio")).rejects.toBeInstanceOf(ApiUnavailableError);
    mocks.fetch.mockRejectedValue(new DOMException("t", "TimeoutError"));
    await expect(userGet("/me/portfolio")).rejects.toBeInstanceOf(ApiUnavailableError);
  });
});
