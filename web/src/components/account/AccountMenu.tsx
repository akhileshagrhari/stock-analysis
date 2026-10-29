"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { authClient } from "@/lib/auth/client";
import s from "./account.module.css";

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
          {/* "Delete account" is wired in Task 12. */}
        </div>
      )}
    </div>
  );
}
