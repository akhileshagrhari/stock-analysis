// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ apiToken: vi.fn(), fetch: vi.fn() }));
vi.mock("@/lib/auth/server", () => ({ apiToken: mocks.apiToken }));
vi.mock("next/headers", () => ({ headers: async () => new Headers({ cookie: "s=1" }) }));

import { DELETE } from "@/app/api/portfolio/route";
import { POST } from "@/app/api/portfolio/import/route";

const ORIGIN = "http://site.test";
const importUrl = `${ORIGIN}/api/portfolio/import?mode=add&filename=holdings.csv`;
const post = (init: { origin?: string | null; body?: string } = {}) =>
  POST(
    new Request(importUrl, {
      method: "POST",
      headers: init.origin === null ? {} : { origin: init.origin ?? ORIGIN },
      body: init.body ?? "a,b\n1,2\n",
    }),
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", mocks.fetch);
  process.env.SA_API_URL = "http://api.test:8000";
  mocks.apiToken.mockResolvedValue("tok");
  mocks.fetch.mockResolvedValue(Response.json({ id: 7 }, { status: 200 }));
});

describe("POST /api/portfolio/import", () => {
  it("refuses another origin", async () => {
    expect((await post({ origin: "http://evil.test" })).status).toBe(403);
    expect((await post({ origin: null })).status).toBe(403);
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("says sign in again without a session", async () => {
    mocks.apiToken.mockResolvedValue(null);
    const res = await post();
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: "Sign in again." });
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("forwards the bytes with the token and passes status and body through", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ detail: { message: "bad", problems: ["x"] } }, { status: 422 }));
    const res = await post({ body: "hello" });
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({ detail: { message: "bad", problems: ["x"] } });
    const [url, init] = mocks.fetch.mock.calls[0];
    expect(url).toBe("http://api.test:8000/v1/me/portfolio/import?mode=add&filename=holdings.csv");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer tok");
    expect(new TextDecoder().decode(init.body)).toBe("hello");
    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("answers 400 for a mode that is neither replace nor add", async () => {
    const res = await POST(new Request(`${ORIGIN}/api/portfolio/import?mode=wipe`, { method: "POST", headers: { origin: ORIGIN }, body: "x" }));
    expect(res.status).toBe(400);
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("answers 413 for a declared length over 1 MB without reading", async () => {
    const res = await POST(
      new Request(importUrl, { method: "POST", headers: { origin: ORIGIN, "content-length": "1000001" }, body: "x" }),
    );
    expect(res.status).toBe(413);
    expect(await res.json()).toEqual({ detail: "The file is larger than 1 MB." });
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("answers 413 for a streamed body over 1 MB with no length, and stops reading", async () => {
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        controller.enqueue(new Uint8Array(400_000));
        if (pulled > 50) controller.close();
      },
    });
    const req = new Request(importUrl, { method: "POST", headers: { origin: ORIGIN }, body: stream, duplex: "half" } as RequestInit);
    const res = await POST(req);
    expect(res.status).toBe(413);
    expect(pulled).toBeLessThan(10);
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("answers 504 when the API times out", async () => {
    mocks.fetch.mockRejectedValue(new DOMException("timed out", "TimeoutError"));
    const res = await post();
    expect(res.status).toBe(504);
    expect((await res.json()).detail).toBe("The server took too long. Check your portfolio before trying again.");
  });

  it("gives the upload a timeout signal", async () => {
    await post();
    expect(mocks.fetch.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });

  it("answers 502 when the API cannot be reached", async () => {
    mocks.fetch.mockRejectedValue(new TypeError("fetch failed"));
    const res = await post();
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ detail: "Couldn't reach the server. Nothing was changed." });
  });
});

describe("DELETE /api/portfolio", () => {
  const del = (origin: string | null = ORIGIN) =>
    DELETE(new Request(`${ORIGIN}/api/portfolio`, { method: "DELETE", headers: origin ? { origin } : {} }));

  it("refuses another origin and a missing session", async () => {
    expect((await del("http://evil.test")).status).toBe(403);
    mocks.apiToken.mockResolvedValue(null);
    expect((await del()).status).toBe(401);
  });

  it("forwards with the token", async () => {
    mocks.fetch.mockResolvedValue(Response.json({ deleted: 3 }));
    const res = await del();
    expect(await res.json()).toEqual({ deleted: 3 });
    const [url, init] = mocks.fetch.mock.calls[0];
    expect(url).toBe("http://api.test:8000/v1/me/portfolio");
    expect(init.method).toBe("DELETE");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer tok");
  });

  it("answers 502 when the API cannot be reached", async () => {
    mocks.fetch.mockRejectedValue(new TypeError("fetch failed"));
    expect((await del()).status).toBe(502);
  });
});
