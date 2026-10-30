import { userSend } from "@/lib/api/user";

// The browser's way to save a portfolio. Only this site's own pages may call
// it (an Origin check, since the session cookie rides along), and the file is
// forwarded as raw bytes with the visitor's token.

const MAX_BYTES = 1_000_000;

const tooBig = () => Response.json({ detail: "The file is larger than 1 MB." }, { status: 413 });

/** The body's bytes, or null once more than MAX_BYTES have arrived (the rest is dropped unread). */
async function readCapped(stream: ReadableStream<Uint8Array> | null): Promise<Uint8Array | null> {
  if (!stream) return new Uint8Array();
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BYTES) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.byteLength;
  }
  return out;
}

export async function POST(request: Request): Promise<Response> {
  if (request.headers.get("origin") !== new URL(request.url).origin) {
    return Response.json({ detail: "Not allowed." }, { status: 403 });
  }
  const query = new URL(request.url).searchParams;
  const mode = query.get("mode");
  if (mode !== "replace" && mode !== "add") return Response.json({ detail: "Pick replace or add." }, { status: 400 });
  // Content-Length is only a hint (chunked uploads have none), so count what arrives.
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_BYTES) return tooBig();
  const body = await readCapped(request.body);
  if (!body) return tooBig();
  const forward = new URLSearchParams({ mode, filename: (query.get("filename") ?? "").slice(0, 200) });
  return userSend(
    `/me/portfolio/import?${forward}`,
    { method: "POST", body: body as BodyInit, headers: { "content-type": "application/octet-stream" } },
    request.headers,
  );
}
