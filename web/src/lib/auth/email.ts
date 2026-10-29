// Server-only: the sign-in code's delivery. Imported by `server.ts`, never by
// a client component — it reads the email service's key.
//
// Resend over plain `fetch`, so there is no SDK to keep current. With no key
// set, development writes the code to the server log instead, so signing in
// locally needs no outside account; production refuses rather than leave a
// visitor waiting for an email that will never come.

const RESEND_URL = "https://api.resend.com/emails";
const TIMEOUT_MS = 10_000;

function isProduction(): boolean {
  return process.env.NODE_ENV === "production";
}

export async function sendSignInCode(email: string, otp: string): Promise<void> {
  const key = process.env.RESEND_API_KEY;
  const from = process.env.EMAIL_FROM;

  if (!key) {
    if (isProduction()) {
      throw new Error("The email service is not configured: set RESEND_API_KEY and EMAIL_FROM.");
    }
    console.info(`[auth] Sign-in code for ${email}: ${otp} (no RESEND_API_KEY, so it was not emailed)`);
    return;
  }
  if (!from) throw new Error("The email service is not configured: set EMAIL_FROM.");

  const res = await fetch(RESEND_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      from,
      to: [email],
      subject: "Your sign-in code",
      text: `Your code is ${otp}. It expires in 5 minutes.`,
    }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`Resend did not accept the sign-in email (HTTP ${res.status}).`);
}

/**
 * A code every sign-in accepts, for local development and the e2e tests —
 * `AUTH_DEV_FIXED_OTP`. Ignored in production, whatever the environment says.
 */
export function fixedOtp(): string | null {
  if (isProduction()) return null;
  return process.env.AUTH_DEV_FIXED_OTP || null;
}
