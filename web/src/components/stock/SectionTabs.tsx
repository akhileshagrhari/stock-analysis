"use client";

import { useEffect, useState } from "react";
import s from "./stock.module.css";

export const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "breakdown", label: "Score breakdown" },
  { id: "history", label: "Score history" },
  { id: "financials", label: "Financials" },
  { id: "news", label: "News" },
  { id: "peers", label: "Peers" },
] as const;

/** In-page anchors with a scroll-spy underline. */
export function SectionTabs({ present }: { present: string[] }) {
  const [current, setCurrent] = useState("overview");
  const key = present.join(",");

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const els = key
      .split(",")
      .map((id) => document.getElementById(id))
      .filter((e): e is HTMLElement => e !== null);
    const io = new IntersectionObserver(
      (entries) => {
        const top = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (top) setCurrent(top.target.id);
      },
      { rootMargin: "-80px 0px -60% 0px" },
    );
    els.forEach((e) => io.observe(e));
    return () => io.disconnect();
  }, [key]);

  return (
    <nav className={s.tabs} aria-label="Sections">
      {SECTIONS.filter((x) => present.includes(x.id)).map((x) => (
        <a key={x.id} href={`#${x.id}`} aria-current={current === x.id ? "true" : undefined} onClick={() => setCurrent(x.id)}>
          {x.label}
        </a>
      ))}
    </nav>
  );
}
