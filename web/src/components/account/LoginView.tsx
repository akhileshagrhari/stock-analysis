"use client";

import { useRouter } from "next/navigation";
import { useState, useSyncExternalStore, type FormEvent } from "react";
import { Card } from "@/components/ui/Card";
import { Notice } from "@/components/ui/Notice";
import { authClient } from "@/lib/auth/client";
import s from "./account.module.css";

const noSubscribe = () => () => {};
const WRONG_CODE = "That code didn't work. Check it or send a new one.";

/**
 * Sign in with Google, or with a six-digit code sent to an email address.
 * Each is offered only when the server has it set up (`google`, `emailCodes`).
 */
export function LoginView({ google, emailCodes, next }: { google: boolean; emailCodes: boolean; next: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // Until the page is interactive a submit would be a plain form post, putting
  // the email in the address bar. Keep the button off until then.
  const ready = useSyncExternalStore(noSubscribe, () => true, () => false);

  async function sendCode(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const { error } = await authClient.emailOtp.sendVerificationOtp({ email: email.trim(), type: "sign-in" });
      if (error) setProblem("We couldn't send a code to that address. Check it and try again.");
      else setSent(true);
    } catch {
      setProblem("We couldn't send a code just now. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const { error } = await authClient.signIn.emailOtp({ email: email.trim(), otp: code.trim() });
      if (error) setProblem(WRONG_CODE);
      else {
        router.push(next);
        router.refresh();
      }
    } catch {
      setProblem(WRONG_CODE);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={s.loginWrap}>
      <Card className={s.loginCard}>
        <h1>Sign in</h1>
        <p className={s.lede}>Sign in to keep a portfolio. Browsing the scores never needs an account.</p>
        {google && (
          <>
            <button type="button" className={s.secondary} onClick={() => authClient.signIn.social({ provider: "google", callbackURL: next })}>
              Continue with Google
            </button>
            {emailCodes && <div className={s.or}>or</div>}
          </>
        )}
        {!emailCodes ? (
          <Notice tone="info">Email sign-in isn&apos;t available right now.</Notice>
        ) : !sent ? (
          <form className={s.form} onSubmit={sendCode}>
            <label className={s.label}>
              Email
              <input className={s.input} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            <button type="submit" className={s.primary} disabled={busy || !ready}>
              Send code
            </button>
          </form>
        ) : (
          <form className={s.form} onSubmit={verify}>
            {/* A failed send can't be detected here (Better Auth only logs it), so no promise that it arrived. */}
            <p className={s.lede}>
              We&apos;ve sent a code to {email.trim()}. It expires in 5 minutes. If it doesn&apos;t arrive in a minute or two, check your spam
              folder or send a new code.
            </p>
            <label className={s.label}>
              Code
              <input
                className={s.input}
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </label>
            <button type="submit" className={s.primary} disabled={busy}>
              Sign in
            </button>
            <button type="button" className={s.link} disabled={busy} onClick={() => sendCode()}>
              Send a new code
            </button>
          </form>
        )}
        {problem && <Notice tone="alert">{problem}</Notice>}
      </Card>
    </div>
  );
}
