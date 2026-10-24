import { apiBase } from "@/lib/api/client";

// The browser's way to the API, so the API needs no CORS. A route handler, not
// a next.config rewrite: `next build` fixes a rewrite's destination, so a
// server started with a different SA_API_URL would send search elsewhere.
//
// It forwards only what browser code calls (search, from SearchBox) rather
// than mirroring the whole API, and gives up after TIMEOUT_MS so a slow API
// can't hold the site's connections open.

const ALLOWED = new Set(["instruments/search"]);
const TIMEOUT_MS = 5000;

export async function GET(request: Request, { params }: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const path = (await params).path.join("/");
  if (!ALLOWED.has(path)) return Response.json({ detail: "Not Found" }, { status: 404 });

  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${apiBase()}/v1/${path}${new URL(request.url).search}`, {
      cache: "no-store",
      signal: AbortSignal.any([request.signal, timeout.signal]),
    });
    // Read the (small) body inside the time limit too.
    const body = await res.arrayBuffer();
    const headers = new Headers({ "content-type": res.headers.get("content-type") ?? "application/json" });
    const cacheControl = res.headers.get("cache-control");
    if (cacheControl) headers.set("cache-control", cacheControl);
    return new Response(body, { status: res.status, headers });
  } catch {
    return timeout.signal.aborted
      ? Response.json({ detail: "API timed out" }, { status: 504 })
      : Response.json({ detail: "API unreachable" }, { status: 502 });
  } finally {
    clearTimeout(timer);
  }
}
