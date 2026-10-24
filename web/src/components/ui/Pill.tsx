import type { ReactNode } from "react";
import s from "./ui.module.css";

export type PillTone = "grey" | "pos" | "neg" | "hold" | "accent";

export function Pill({ tone = "grey", children, title, ...data }: { tone?: PillTone; children: ReactNode; title?: string } & Record<`data-${string}`, string>) {
  return (
    <span className={`${s.pill} ${s[tone]}`} title={title} {...data}>
      {children}
    </span>
  );
}
