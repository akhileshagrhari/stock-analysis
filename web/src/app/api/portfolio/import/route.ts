import { userSend } from "@/lib/api/user";

// The browser's way to save a portfolio. Only this site's own pages may call
// it (an Origin check, since the session cookie rides along), and the file is
// forwarded as raw bytes with the visitor's token.

const MAX_BYTES = 1_000_000;

export async function POST(request: Request): Promise<Response> {
  if (request.headers.get("origin") !== new URL(request.url).origin) {
    return Response.json({ detail: "Not allowed." }, { status: 403 });
  }
  const query = new URL(request.url).searchParams;
  const mode = query.get("mode");
  if (mode !== "replace" && mode !== "add") return Response.json({ detail: "Pick replace or add." }, { status: 400 });
  const length = Number(request.headers.get("content-length") ?? 0);
  if (length > MAX_BYTES) return Response.json({ detail: "The file is larger than 1 MB." }, { status: 413 });

  const body = await request.arrayBuffer();
  const forward = new URLSearchParams({ mode, filename: (query.get("filename") ?? "").slice(0, 200) });
  return userSend(
    `/me/portfolio/import?${forward}`,
    { method: "POST", body, headers: { "content-type": "application/octet-stream" } },
    request.headers,
  );
}
