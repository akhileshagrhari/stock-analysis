// The API calls that need a signed-in visitor. Server only: the token is read
// from the request's cookies here, so the browser never holds one.

import { headers } from "next/headers";
import { apiToken } from "@/lib/auth/server";
import { ApiUnavailableError, apiBase } from "./client";

export class SignedOutError extends Error {
  override name = "SignedOutError";
  constructor() {
    super("Not signed in");
  }
}

/**
 * Like `serverGet`, for a route that needs the visitor. Throws SignedOutError
 * with no session or on a 401; a 404 that is the API's own is null.
 */
export async function userGet<T>(path: string): Promise<T | null> {
  const token = await apiToken(await headers());
  if (!token) throw new SignedOutError();
  let res: Response;
  try {
    res = await fetch(`${apiBase()}/v1${path}`, { cache: "no-store", headers: { authorization: `Bearer ${token}` } });
  } catch (cause) {
    throw new ApiUnavailableError(null, path, { cause });
  }
  if (res.status === 401) throw new SignedOutError();
  if (res.status === 404) {
    // The API's "Not Found" (a route not deployed yet) and its own 404s both
    // mean "nothing there" for a visitor's own data.
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") return null;
    } catch {
      /* not the API */
    }
    throw new ApiUnavailableError(404, path);
  }
  if (!res.ok) throw new ApiUnavailableError(res.status, path);
  return (await res.json()) as T;
}

const down = () => Response.json({ detail: "Couldn't reach the server. Nothing was changed." }, { status: 502 });

/**
 * For route handlers: send `init` to `/v1${path}` with the visitor's token and
 * return the API's response untouched. No session is a 401; an unreachable
 * API is a 502.
 */
export async function userSend(path: string, init: RequestInit, requestHeaders: Headers): Promise<Response> {
  const token = await apiToken(requestHeaders);
  if (!token) return Response.json({ detail: "Sign in again." }, { status: 401 });
  try {
    const res = await fetch(`${apiBase()}/v1${path}`, {
      ...init,
      cache: "no-store",
      headers: { ...(init.headers as Record<string, string> | undefined), authorization: `Bearer ${token}` },
    });
    return new Response(await res.arrayBuffer(), {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
    });
  } catch {
    return down();
  }
}
