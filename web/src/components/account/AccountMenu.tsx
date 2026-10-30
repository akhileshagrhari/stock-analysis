"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Notice } from "@/components/ui/Notice";
import { authClient } from "@/lib/auth/client";
import { PORTFOLIO_NOT_DELETED } from "@/lib/auth/config";
import s from "./account.module.css";

const NOT_FRESH = "For your security, sign out and sign in again, then delete your account.";
const NOTHING_DELETED = "Couldn't delete your portfolio, so nothing was deleted. Try again later.";
const NOT_DELETED = "Couldn't delete your account. Try again later.";

/** What to say when `deleteUser` answers with an error `code` (none when the request itself failed). */
function deleteProblem(code: string | undefined): string {
  // Better Auth refuses a session older than its freshAge (a day) before anything is deleted.
  if (code === "SESSION_EXPIRED") return NOT_FRESH;
  if (code === PORTFOLIO_NOT_DELETED) return NOTHING_DELETED;
  return NOT_DELETED;
}

/**
 * Sign in, or the avatar and its menu. Reads the session in the browser so the
 * pages around it need no cookies and stay cacheable.
 *
 * The outer component returns before the hook when accounts are off, so a site
 * without them never builds the auth client's session store.
 */
export function AccountMenu({ enabled }: { enabled: boolean }) {
  if (!enabled) return null;
  return <Menu />;
}

function Menu() {
  const { data, isPending } = authClient.useSession();
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const deleting = useRef(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  if (isPending) return <div className={s.menuWrap} style={{ width: 38, height: 38 }} aria-hidden="true" />;
  if (!data) {
    return (
      <Link className={s.signIn} href={`/login?next=${encodeURIComponent(pathname)}`}>
        Sign in
      </Link>
    );
  }

  /**
   * One request. On the server, Better Auth deletes the portfolio first (its
   * `beforeDelete` hook, through the API) and then the account, or keeps
   * both: an account is never deleted with its holdings left behind.
   */
  async function deleteAccount() {
    if (deleting.current || !confirm("Delete your account and portfolio? This can't be undone.")) return;
    deleting.current = true;
    setProblem(null);
    const { error } = await authClient.deleteUser().catch(() => ({ error: { code: undefined } }));
    deleting.current = false;
    if (error) {
      setProblem(deleteProblem(error.code));
      return;
    }
    setOpen(false);
    router.push("/");
    router.refresh();
  }

  const label = (data.user.name || data.user.email || "?").trim();
  const letter = label.charAt(0).toUpperCase() || "?";

  return (
    <div className={s.menuWrap} ref={wrap}>
      <button type="button" className={s.avatar} aria-label="Account" aria-expanded={open} aria-haspopup="true" onClick={() => setOpen((v) => !v)}>
        {letter}
      </button>
      {open && (
        <div className={s.menu}>
          <div className={s.who}>{data.user.email}</div>
          <Link href="/portfolio" onClick={() => setOpen(false)}>
            Portfolio
          </Link>
          <button
            type="button"
            onClick={async () => {
              await authClient.signOut();
              setOpen(false);
              router.push("/");
              router.refresh();
            }}
          >
            Sign out
          </button>
          <button type="button" className={s.danger} onClick={deleteAccount}>
            Delete account
          </button>
          {problem && (
            <Notice tone="alert" className={s.menuNotice}>
              {problem}
            </Notice>
          )}
        </div>
      )}
    </div>
  );
}
