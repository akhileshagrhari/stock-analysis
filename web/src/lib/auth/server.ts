// Server-only: the Better Auth instance and the two questions server code asks
// it — who is signed in, and what token proves it to the API. Never import this
// from a client component; `client.ts` is the browser's side.
//
// Accounts live in Postgres schema `auth` (created by `stockanalysis
// setup-accounts`, tables by `npm run auth:migrate`), never in `site`, which
// every publish replaces. The pool pins `search_path` so Better Auth's
// unqualified table names land there.
//
// Relative imports, not `@/`, so the Better Auth CLI (`npm run auth:migrate`)
// can load this file to read the schema without knowing the site's path alias.
// The CLI loads `.env` and `.env.local` first, so `auth` is built for it.

import { betterAuth } from "better-auth";
import { APIError, isAPIError } from "better-auth/api";
import { nextCookies } from "better-auth/next-js";
import { emailOTP } from "better-auth/plugins/email-otp";
import { jwt } from "better-auth/plugins/jwt";
import { headers } from "next/headers";
import { Pool } from "pg";

import { apiBase } from "../api/client";
import { accountsEnabled, PORTFOLIO_NOT_DELETED } from "./config";
import { emailCodesAvailable, fixedOtp, sendSignInCode } from "./email";

// The site's own origin. The API checks a token's `iss` and `aud` against it
// (SA_AUTH_ISSUER / SA_AUTH_AUDIENCE) and fetches the keys from
// `${BASE}/api/auth/jwks` (SA_AUTH_JWKS_URL).
// `||`, not `??`: an empty BETTER_AUTH_URL would mint tokens with no issuer.
const BASE = process.env.BETTER_AUTH_URL || "http://localhost:3000";

const google =
  process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET
    ? { clientId: process.env.GOOGLE_CLIENT_ID, clientSecret: process.env.GOOGLE_CLIENT_SECRET }
    : null;

const API_TIMEOUT_MS = 10_000;

/** Why `beforeDelete` kept the account; the account menu reads the code. */
function portfolioNotDeleted(why: string): APIError {
  console.error(`[auth] Account kept: its portfolio could not be deleted first (${why}).`);
  return APIError.from("SERVICE_UNAVAILABLE", {
    code: PORTFOLIO_NOT_DELETED,
    message: "Couldn't delete the portfolio, so the account was kept.",
  });
}

/**
 * Better Auth's `beforeDelete`: the portfolio goes first, through the API with
 * the visitor's own token, so an account is never deleted with its holdings
 * left behind — however `/api/auth/delete-user` was called. Throwing keeps
 * the account.
 *
 * Better Auth runs this after its freshness check (a session older than
 * `session.freshAge` is refused with SESSION_EXPIRED first), so a stale
 * session never deletes the portfolio either. The session is still live
 * here, so the token is minted from the request's cookie exactly as for any
 * other API call. Server code calling `auth.api.deleteUser` without a request
 * has no cookie to mint it from, and is refused.
 */
async function deletePortfolioFirst(_user: unknown, request?: Request): Promise<void> {
  if (!request) throw portfolioNotDeleted("no request to mint the API token from");
  let token: string | null;
  try {
    token = await apiToken(request.headers);
  } catch (error) {
    throw portfolioNotDeleted(`minting the API token failed: ${String(error)}`);
  }
  if (!token) throw portfolioNotDeleted("no session to mint the API token from");
  let res: Response;
  try {
    res = await fetch(`${apiBase()}/v1/me/portfolio`, {
      method: "DELETE",
      cache: "no-store",
      headers: { authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
  } catch (error) {
    throw portfolioNotDeleted(`the API could not be reached: ${String(error)}`);
  }
  if (!res.ok) throw portfolioNotDeleted(`the API answered HTTP ${res.status}`);
}

function createAuth() {
  // Production without an email service offers no email codes at all, rather
  // than a form that says a code was sent when none can be (Better Auth only
  // logs a failed send). Logged, not thrown: `next build` runs this with
  // NODE_ENV=production and no reason to have the email keys — and stays
  // quiet there, where every build worker would repeat it; the server logs it
  // when it starts.
  const emailCodes = emailCodesAvailable();
  if (!emailCodes && process.env.NEXT_PHASE !== "phase-production-build") {
    console.error(
      "[auth] Email sign-in is off: set RESEND_API_KEY and EMAIL_FROM to send sign-in codes. " +
        "Until then only Google sign-in is offered, if it is set up.",
    );
  }
  const fixed = fixedOtp();
  return betterAuth({
    database: new Pool({ connectionString: process.env.DATABASE_URL, options: "-c search_path=auth" }),
    // Google only when its keys are set: local development needs no outside account.
    socialProviders: google ? { google } : {},
    user: { deleteUser: { enabled: true, beforeDelete: deletePortfolioFirst } },
    plugins: [
      ...(emailCodes
        ? [
            emailOTP({
              // The email says "It expires in 5 minutes"; this keeps it true.
              expiresIn: 300,
              // Only a hash in the database: a leaked table holds no usable code.
              storeOTP: "hashed",
              sendVerificationOTP: ({ email, otp }) => sendSignInCode(email, otp),
              // Spread only when set: an explicit `generateOTP: undefined`
              // replaces Better Auth's own generator, and every send fails.
              ...(fixed ? { generateOTP: () => fixed } : {}),
            }),
          ]
        : []),
      // EdDSA keys by default, which is the only algorithm the API accepts.
      // The token is minted server-side only (`apiToken`); without
      // `disableSettingJwtHeader` every /get-session would also hand one to
      // the browser in a `set-auth-jwt` header.
      jwt({ disableSettingJwtHeader: true, jwt: { issuer: BASE, audience: BASE, expirationTime: "15m" } }),
      // Last, so it sees the cookies every other plugin sets.
      nextCookies(),
    ],
  });
}

export type Auth = ReturnType<typeof createAuth>;

/**
 * Better Auth, or null when accounts are off (`accountsEnabled()`).
 *
 * Built at import only when accounts are on. Better Auth starts up the moment
 * it is constructed; without a secret in production that startup rejects with
 * nothing awaiting it, which Next logs as an unhandled rejection on every
 * build and on any request that loads this module; and without DATABASE_URL
 * its pool would fall back to pg's defaults (the PG* variables, the local
 * socket). So a site without accounts never constructs it, and callers
 * handle null.
 */
export const auth: Auth | null = accountsEnabled() ? createAuth() : null;

/** The signed-in visitor, or null — also null when accounts are switched off. */
export async function currentUser(): Promise<{ id: string; email: string; name: string } | null> {
  if (!auth) return null;
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session) return null;
  const { id, email, name } = session.user;
  return { id, email, name };
}

/**
 * A short-lived JWT for the API's `Authorization: Bearer`, minted from the
 * session cookie in `requestHeaders`; null when there is no session.
 */
export async function apiToken(requestHeaders: Headers): Promise<string | null> {
  if (!auth) return null;
  try {
    return (await auth.api.getToken({ headers: requestHeaders })).token;
  } catch (error) {
    // No session is an answer, not a failure. Anything else (the database
    // being down, say) is for the caller to report.
    if (isAPIError(error) && error.statusCode === 401) return null;
    throw error;
  }
}
