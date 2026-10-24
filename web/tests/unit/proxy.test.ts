// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/api/v1/[...path]/route";

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("/api/v1 proxy", () => {
  it("reads SA_API_URL when the request arrives, not when the app was built", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => Response.json([{ nse_symbol: "RELIANCE" }]));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("SA_API_URL", "http://api.internal:9000");
    const res = await GET(new Request("http://site/api/v1/instruments/search?q=rel&limit=8"), ctx("instruments", "search"));
    expect(fetchMock.mock.calls[0][0]).toBe("http://api.internal:9000/v1/instruments/search?q=rel&limit=8");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual([{ nse_symbol: "RELIANCE" }]);
  });

  it("passes the API's status through", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "Not Found" }, { status: 404 })));
    const res = await GET(new Request("http://site/api/v1/instruments/by-symbol/NOPE"), ctx("instruments", "by-symbol", "NOPE"));
    expect(res.status).toBe(404);
  });

  it("answers 502 when the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("fetch failed");
    }));
    const res = await GET(new Request("http://site/api/v1/health"), ctx("health"));
    expect(res.status).toBe(502);
  });

  it("refuses dot segments", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const res = await GET(new Request("http://site/api/v1/x"), ctx("instruments", ".."));
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
