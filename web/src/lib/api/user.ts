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
 * Logs why the API refused a token this server minted a moment ago from a
 * live session. That is a configuration fault, not a signed-out visitor:
 * sending them to sign in would loop, since /login sends a signed-in visitor
 * straight back. So callers report it as an outage instead.
 */
function logRefusedToken(path: string): void {
  console.error(
    // The path without its query, which can carry the visitor's file name.
    `[auth] The API refused a freshly minted token for ${path.split("?")[0]} (HTTP 401). Check that SA_AUTH_ISSUER and ` +
      "SA_AUTH_AUDIENCE match BETTER_AUTH_URL, that SA_AUTH_JWKS_URL is this site's /api/auth/jwks, " +
      "and that the two servers' clocks agree.",
  );
}

/**
 * Like `serverGet`, for a route that needs the visitor. Throws SignedOutError
 * only when there is no session to mint a token from; the API refusing the
 * token is an ApiUnavailableError (see `logRefusedToken`). A 404 that is the
 * API's own is null.
 */
export async function userGet<T>(path: string): Promise<T | null> {
  const token = await apiToken(await headers());
  if (!token) throw new SignedOutError();
  let res: Response;
  try {
    res = await fetch(`${apiBase()}/v1${path}`, {
      cache: "no-store",
      headers: { authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    });
  } catch (cause) {
    throw new ApiUnavailableError(null, path, { cause });
  }
  if (res.status === 401) {
    logRefusedToken(path);
    throw new ApiUnavailableError(401, path);
  }
  if (res.status === 404) {
    // The API's own "no such thing" is a value; FastAPI's bare "Not Found"
    // (a wrong path or an undeployed route) is an outage, as in serverGet.
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail !== "Not Found") return null;
    } catch {
      /* not the API */
    }
    throw new ApiUnavailableError(404, path);
  }
  if (!res.ok) throw new ApiUnavailableError(res.status, path);
  return (await res.json()) as T;
}

/** Matches the /api/v1 proxy. */
const READ_TIMEOUT_MS = 5000;
const UPLOAD_TIMEOUT_MS = 30_000;

const down = () => Response.json({ detail: "Couldn't reach the server. Nothing was changed." }, { status: 502 });
// The API answers 401 before the route runs, so nothing was changed.
const refused = () =>
  Response.json(
    { detail: "The server couldn't confirm your sign-in. Nothing was changed. Try again later." },
    { status: 502, headers: { "cache-control": "no-store" } },
  );

/**
 * For route handlers: send `init` to `/v1${path}` with the visitor's token and
 * return the API's response untouched. No session is a 401, which sends the
 * browser to sign in; the API refusing the token is a 502, which doesn't (see
 * `logRefusedToken`); an unreachable API is a 502.
 */
export async function userSend(
  path: string,
  init: RequestInit,
  requestHeaders: Headers,
  timeoutMs = UPLOAD_TIMEOUT_MS,
): Promise<Response> {
  const token = await apiToken(requestHeaders);
  if (!token) return Response.json({ detail: "Sign in again." }, { status: 401 });
  try {
    const res = await fetch(`${apiBase()}/v1${path}`, {
      ...init,
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
      headers: { ...(init.headers as Record<string, string> | undefined), authorization: `Bearer ${token}` },
    });
    if (res.status === 401) {
      logRefusedToken(path);
      return refused();
    }
    return new Response(await res.arrayBuffer(), {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
    });
  } catch (error) {
    // The API may have committed before the clock ran out, so don't promise otherwise.
    if (error instanceof Error && error.name === "TimeoutError") {
      return Response.json({ detail: "The server took too long. Check your portfolio before trying again." }, { status: 504 });
    }
    return down();
  }
}
