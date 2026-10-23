import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiUnavailableError, browserGet, serverGet } from "@/lib/api/client";

const realFetch = global.fetch;
afterEach(() => {
  global.fetch = realFetch;
});

function stubFetch(status: number, body: unknown) {
  const f = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
  global.fetch = f as unknown as typeof fetch;
  return f;
}

describe("serverGet", () => {
  it("maps 404 to null", async () => {
    stubFetch(404, { detail: "No signal stored" });
    expect(await serverGet("/instruments/X/explanation")).toBeNull();
  });

  it("throws ApiUnavailableError on 503", async () => {
    stubFetch(503, {});
    await expect(serverGet("/health")).rejects.toMatchObject({ name: "ApiUnavailableError", status: 503 });
  });

  it("throws ApiUnavailableError when fetch rejects", async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError("fetch failed")) as unknown as typeof fetch;
    await expect(serverGet("/health")).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("prefixes /v1 and SA_API_URL", async () => {
    const f = stubFetch(200, []);
    await serverGet("/sectors");
    expect(f.mock.calls[0][0]).toBe("http://localhost:8000/v1/sectors");
  });

  it("returns the parsed body on 200", async () => {
    stubFetch(200, { status: "ok" });
    expect(await serverGet("/health")).toEqual({ status: "ok" });
  });
});

describe("browserGet", () => {
  it("goes through the /api/v1 proxy and maps 404 to null", async () => {
    const f = stubFetch(404, {});
    expect(await browserGet("/instruments/search?q=x")).toBeNull();
    expect(f.mock.calls[0][0]).toBe("/api/v1/instruments/search?q=x");
  });

  it("throws ApiUnavailableError on 500", async () => {
    stubFetch(500, {});
    await expect(browserGet("/health")).rejects.toBeInstanceOf(ApiUnavailableError);
  });
});
