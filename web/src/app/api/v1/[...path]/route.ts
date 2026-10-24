import { apiBase } from "@/lib/api/client";

// The browser's way to the API, so the API needs no CORS. A route handler, not
// a next.config rewrite: `next build` fixes a rewrite's destination, so a
// server started with a different SA_API_URL would send search elsewhere.
export async function GET(request: Request, { params }: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await params;
  if (path.some((p) => p === "." || p === "..")) return new Response(null, { status: 404 });

  const url = `${apiBase()}/v1/${path.map(encodeURIComponent).join("/")}${new URL(request.url).search}`;
  let res: Response;
  try {
    res = await fetch(url, { cache: "no-store", signal: request.signal });
  } catch {
    return Response.json({ detail: "API unreachable" }, { status: 502 });
  }
  const headers = new Headers({ "content-type": res.headers.get("content-type") ?? "application/json" });
  const cacheControl = res.headers.get("cache-control");
  if (cacheControl) headers.set("cache-control", cacheControl);
  return new Response(res.body, { status: res.status, headers });
}
