"use client";

import { useId, useState } from "react";
import s from "./ui.module.css";

/**
 * An ⓘ button that shows its explanation as text: tap or click to open, tap
 * again or Escape to close. A button, not a hover tooltip, so it works on
 * touch; the hit target is 24px.
 */
export function InfoTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className={s.infoWrap}>
      <button
        type="button"
        className={s.info}
        aria-label="What this means"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
      >
        <span aria-hidden="true">i</span>
      </button>
      {open && (
        <span id={id} role="note" className={s.infoText}>
          {text}
        </span>
      )}
    </span>
  );
}
