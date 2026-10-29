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
import { isAPIError } from "better-auth/api";
import { nextCookies } from "better-auth/next-js";
import { emailOTP } from "better-auth/plugins/email-otp";
import { jwt } from "better-auth/plugins/jwt";
import { headers } from "next/headers";
import { Pool } from "pg";

import { accountsEnabled } from "./config";
import { fixedOtp, sendSignInCode } from "./email";

// The site's own origin. The API checks a token's `iss` and `aud` against it
// (SA_AUTH_ISSUER / SA_AUTH_AUDIENCE) and fetches the keys from
// `${BASE}/api/auth/jwks` (SA_AUTH_JWKS_URL).
// `||`, not `??`: an empty BETTER_AUTH_URL would mint tokens with no issuer.
const BASE = process.env.BETTER_AUTH_URL || "http://localhost:3000";

const google =
  process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET
    ? { clientId: process.env.GOOGLE_CLIENT_ID, clientSecret: process.env.GOOGLE_CLIENT_SECRET }
    : null;

function createAuth() {
  return betterAuth({
    database: new Pool({ connectionString: process.env.DATABASE_URL, options: "-c search_path=auth" }),
    // Google only when its keys are set: local development needs no outside account.
    socialProviders: google ? { google } : {},
    user: { deleteUser: { enabled: true } },
    plugins: [
      emailOTP({
        // The email says "It expires in 5 minutes"; this keeps it true.
        expiresIn: 300,
        sendVerificationOTP: ({ email, otp }) => sendSignInCode(email, otp),
        generateOTP: fixedOtp() ? () => fixedOtp()! : undefined,
      }),
      // EdDSA keys by default, which is the only algorithm the API accepts.
      jwt({ jwt: { issuer: BASE, audience: BASE, expirationTime: "15m" } }),
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
