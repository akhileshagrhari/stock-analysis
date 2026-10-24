import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiUnavailableError, browserGet, serverGet } from "@/lib/api/client";

const realFetch = global.fetch;
afterEach(() => {
  global.fetch = realFetch;
  vi.unstubAllEnvs();
});

function stubFetch(status: number, body: unknown) {
  const f = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
  global.fetch = f as unknown as typeof fetch;
  return f;
}

describe("serverGet", () => {
  it("maps the API's own 404 answer to null", async () => {
    stubFetch(404, { detail: "No signal stored for 'INE040A01034'" });
    expect(await serverGet("/instruments/X/explanation")).toBeNull();
  });

  it("treats a 404 for an unknown route as an outage, not as absent data", async () => {
    stubFetch(404, { detail: "Not Found" });
    await expect(serverGet("/instruments/by-symbol/RELIANCE")).rejects.toMatchObject({ name: "ApiUnavailableError", status: 404 });
  });

  it("treats a 404 without the API's JSON detail as an outage", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("<html>404</html>", { status: 404, headers: { "content-type": "text/html" } })) as unknown as typeof fetch;
    await expect(serverGet("/health")).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("ignores trailing slashes on SA_API_URL", async () => {
    vi.stubEnv("SA_API_URL", "http://localhost:8000//");
    const f = stubFetch(200, []);
    await serverGet("/sectors");
    expect(f.mock.calls[0][0]).toBe("http://localhost:8000/v1/sectors");
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
  it("goes through the /api/v1 proxy and maps the API's own 404 to null", async () => {
    const f = stubFetch(404, { detail: "No instrument for symbol 'X'" });
    expect(await browserGet("/instruments/search?q=x")).toBeNull();
    expect(f.mock.calls[0][0]).toBe("/api/v1/instruments/search?q=x");
  });

  it("throws ApiUnavailableError on 500", async () => {
    stubFetch(500, {});
    await expect(browserGet("/health")).rejects.toBeInstanceOf(ApiUnavailableError);
  });
});
