import type { ReactNode } from "react";
import s from "./ui.module.css";

/** An inline notice: warn (amber), alert (red, announced) or info (grey). */
export function Notice({ tone, children, className }: { tone: "warn" | "alert" | "info"; children: ReactNode; className?: string }) {
  const toneClass = tone === "info" ? s.info2 : s[tone];
  return (
    <div className={[s.notice, toneClass, className ?? ""].join(" ")} role={tone === "alert" ? "alert" : undefined}>
      {children}
    </div>
  );
}
