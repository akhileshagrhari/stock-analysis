"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { Notice } from "@/components/ui/Notice";
import s from "./portfolio.module.css";

/** A quiet "Delete portfolio" at the foot of the page, asked twice. */
export function DangerZone() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // State updates land after a second click can already have fired.
  const inFlight = useRef(false);

  async function remove() {
    if (inFlight.current || !confirm("Delete your saved portfolio? This can't be undone.")) return;
    inFlight.current = true;
    setBusy(true);
    setFailed(false);
    try {
      const res = await fetch("/api/portfolio", { method: "DELETE" });
      if (res.status === 401) router.push("/login?next=/portfolio");
      else if (res.ok) router.refresh();
      else setFailed(true);
    } catch {
      setFailed(true);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className={s.dangerZone}>
      <button type="button" className={s.quiet} onClick={remove} disabled={busy}>
        Delete portfolio
      </button>
      {failed && (
        <Notice tone="alert" className={s.dangerNotice}>
          Couldn&apos;t delete your portfolio. Try again.
        </Notice>
      )}
    </div>
  );
}
