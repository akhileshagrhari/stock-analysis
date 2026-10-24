// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/api/v1/[...path]/route";

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });
const search = (q = "rel") => new Request(`http://site/api/v1/instruments/search?q=${q}&limit=8`);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe("/api/v1 proxy", () => {
  it("reads SA_API_URL when the request arrives, not when the app was built", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => Response.json([{ nse_symbol: "RELIANCE" }]));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("SA_API_URL", "http://api.internal:9000/");
    const res = await GET(search(), ctx("instruments", "search"));
    expect(fetchMock.mock.calls[0][0]).toBe("http://api.internal:9000/v1/instruments/search?q=rel&limit=8");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual([{ nse_symbol: "RELIANCE" }]);
  });

  it("passes the API's status through", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "q must not be blank" }, { status: 422 })));
    const res = await GET(search("%20"), ctx("instruments", "search"));
    expect(res.status).toBe(422);
  });

  it("answers 502 when the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("fetch failed");
    }));
    const res = await GET(search(), ctx("instruments", "search"));
    expect(res.status).toBe(502);
  });

  it("answers 504 when the API takes longer than 5 seconds", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    })));
    const pending = GET(search(), ctx("instruments", "search"));
    await vi.advanceTimersByTimeAsync(5000);
    expect((await pending).status).toBe(504);
  });

  it("forwards only what the browser uses: search", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const path of [["health"], ["signals"], ["instruments", "by-symbol", "RELIANCE"], ["instruments", ".."]]) {
      const res = await GET(new Request(`http://site/api/v1/${path.join("/")}`), ctx(...path));
      expect(res.status).toBe(404);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
