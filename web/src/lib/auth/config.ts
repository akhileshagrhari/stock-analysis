// Accounts: whether they are switched on, where to send someone after they
// sign in, and the one error code the server and the account menu share. No
// Better Auth import here, so any component can ask.

/**
 * The error code `/api/auth/delete-user` answers with when the portfolio
 * couldn't be deleted first, so the account was kept too. Here so the account
 * menu (a client component) and `server.ts` share it.
 */
export const PORTFOLIO_NOT_DELETED = "PORTFOLIO_NOT_DELETED";

/** Where a sign-in lands when it was not sent from anywhere in particular. */
const DEFAULT_NEXT = "/portfolio";

/** True when the site has a database for accounts and a secret to sign them with. */
export function accountsEnabled(): boolean {
  return Boolean(process.env.DATABASE_URL && process.env.BETTER_AUTH_SECRET);
}

// Any origin will do: the URL is parsed against it only to see whether `raw`
// tries to leave it. `.invalid` is reserved, so it can't be a real host.
const PROBE = "http://site.invalid";

/**
 * A `next` parameter made safe to redirect to: a path on this site, or
 * `/portfolio`.
 *
 * Parsed the way a browser would read it, rather than pattern-matched, because
 * a browser treats `/\evil.test` as `//evil.test`, drops tabs and newlines,
 * and collapses `/.//` — each a way to name another host while starting with
 * one slash.
 */
export function safeNext(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/")) return DEFAULT_NEXT;
  let url: URL;
  try {
    url = new URL(raw, PROBE);
  } catch {
    return DEFAULT_NEXT;
  }
  if (url.origin !== PROBE) return DEFAULT_NEXT;
  const path = `${url.pathname}${url.search}${url.hash}`;
  // A path that now starts with two slashes would name a host when used as a
  // Location header.
  return /^\/[/\\]/.test(path) ? DEFAULT_NEXT : path;
}
