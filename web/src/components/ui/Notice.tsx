import type { ReactNode } from "react";
import s from "./ui.module.css";

/** An inline notice: warn (amber), alert (red, announced), success (green, announced politely) or info (grey). */
export function Notice({ tone, children, className }: { tone: "warn" | "alert" | "success" | "info"; children: ReactNode; className?: string }) {
  const toneClass = tone === "info" ? s.info2 : s[tone];
  const role = tone === "alert" ? "alert" : tone === "success" ? "status" : undefined;
  return (
    <div className={[s.notice, toneClass, className ?? ""].join(" ")} role={role}>
      {children}
    </div>
  );
}
