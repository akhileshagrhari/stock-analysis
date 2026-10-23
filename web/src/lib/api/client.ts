// The one way pages read the API. A 404 is a value (null), because "this
// company has no score" is a state the page renders. Anything else that goes
// wrong — a 5xx during a publish, a refused connection — is an outage, and
// throws so the route's error boundary can say so.

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

async function request<T>(url: string, path: string, init: RequestInit): Promise<T | null> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (cause) {
    if (init.signal?.aborted) throw cause;
    throw new ApiUnavailableError(null, path, { cause });
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiUnavailableError(res.status, path);
  return (await res.json()) as T;
}

/** Server components: straight to the API. `path` starts after /v1. */
export function serverGet<T>(path: string): Promise<T | null> {
  const base = process.env.SA_API_URL ?? "http://localhost:8000";
  return request<T>(`${base}/v1${path}`, path, { next: { revalidate: REVALIDATE_SECONDS } });
}

/** Browser: through the /api/v1 rewrite, so the API needs no CORS. */
export function browserGet<T>(path: string, signal?: AbortSignal): Promise<T | null> {
  return request<T>(`/api/v1${path}`, path, { signal });
}

export type Getter = <T>(path: string) => Promise<T | null>;
