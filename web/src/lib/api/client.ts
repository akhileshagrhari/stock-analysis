// The one way pages read the API. The API's own 404 ("No signal stored for
// …") is a value (null), because "this company has no score" is a state the
// page renders. Anything else that goes wrong — a 5xx during a publish, a
// refused connection, a 404 for a route that doesn't exist — is an outage,
// and throws so the route's error boundary can say so.

export class ApiUnavailableError extends Error {
  override name = "ApiUnavailableError";
  constructor(
    readonly status: number | null,
    readonly path: string,
    options?: { cause?: unknown },
  ) {
    super(`API unavailable (${status ?? "no response"}) for ${path}`, options);
  }
}

/** Seconds a server-side response is reused; matches the API's Cache-Control. */
export const REVALIDATE_SECONDS = 300;

/**
 * True when a 404 is the API answering "no such thing". FastAPI's answer for
 * an unknown route (a wrong SA_API_URL, an endpoint not deployed yet) is
 * {"detail": "Not Found"}; anything without a JSON detail came from
 * something other than the API.
 */
async function isAbsent(res: Response): Promise<boolean> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === "string" && body.detail !== "Not Found";
  } catch {
    return false;
  }
}

async function request<T>(url: string, path: string, init: RequestInit): Promise<T | null> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (cause) {
    if (init.signal?.aborted) throw cause;
    throw new ApiUnavailableError(null, path, { cause });
  }
  if (res.status === 404) {
    if (await isAbsent(res)) return null;
    throw new ApiUnavailableError(404, path);
  }
  if (!res.ok) throw new ApiUnavailableError(res.status, path);
  return (await res.json()) as T;
}

/** Where the API is, read on every call so a running server follows SA_API_URL. */
export function apiBase(): string {
  return (process.env.SA_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
}

/** Server components: straight to the API. `path` starts after /v1. */
export function serverGet<T>(path: string): Promise<T | null> {
  return request<T>(`${apiBase()}/v1${path}`, path, { next: { revalidate: REVALIDATE_SECONDS } });
}

/** Browser: through the /api/v1 proxy route, so the API needs no CORS. */
export function browserGet<T>(path: string, signal?: AbortSignal): Promise<T | null> {
  return request<T>(`/api/v1${path}`, path, { signal });
}

export type Getter = <T>(path: string) => Promise<T | null>;
