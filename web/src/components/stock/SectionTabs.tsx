"use client";

import { useEffect, useRef, useState } from "react";
import s from "./stock.module.css";

export const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "breakdown", label: "Score breakdown" },
  { id: "history", label: "Score history" },
  { id: "financials", label: "Financials" },
  { id: "news", label: "News" },
  { id: "peers", label: "Peers" },
] as const;

/**
 * The section a reader is in: the one whose top most recently passed `line`
 * (px from the viewport top). Page order isn't tab order, since News sits
 * beside the breakdown, so this compares positions, not list order. Side-by-
 * side sections tie; the current one is kept, so clicking News doesn't flip
 * the underline back to Score breakdown.
 */
export function pickSection(ids: string[], tops: Record<string, number | undefined>, line: number, prev: string): string {
  let best = -Infinity;
  for (const id of ids) {
    const top = tops[id];
    if (top !== undefined && top <= line && top > best) best = top;
  }
  const tied = ids.filter((id) => tops[id] !== undefined && Math.abs(tops[id]! - best) < 2);
  return tied.includes(prev) ? prev : (tied[0] ?? ids[0]);
}

/** In-page anchors with a scroll-spy underline. */
export function SectionTabs({ present }: { present: string[] }) {
  const [current, setCurrent] = useState("overview");
  const key = present.join(",");
  // After a tab click the page scrolls to it; hold that choice meanwhile.
  const lockUntil = useRef(0);

  useEffect(() => {
    // The current section is the one whose top most recently scrolled past
    // the sticky tabs. Six getBoundingClientRect calls per scroll event is
    // cheap, and React skips the render when the section hasn't changed.
    const ids = key.split(",");
    const update = () => {
      if (Date.now() < lockUntil.current) return;
      // At the foot of the page the last sections can't reach the line, so
      // everything on screen counts as reached.
      const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      const line = atBottom ? window.innerHeight : 140;
      const tops: Record<string, number | undefined> = {};
      for (const id of ids) tops[id] = document.getElementById(id)?.getBoundingClientRect().top;
      setCurrent((prev) => pickSection(ids, tops, line, prev));
    };
    window.addEventListener("scroll", update, { passive: true });
    return () => window.removeEventListener("scroll", update);
  }, [key]);

  return (
    <nav className={s.tabs} aria-label="Sections">
      {SECTIONS.filter((x) => present.includes(x.id)).map((x) => (
        <a key={x.id} href={`#${x.id}`} aria-current={current === x.id ? "true" : undefined} onClick={() => {
            setCurrent(x.id);
            lockUntil.current = Date.now() + 1000;
          }}>
          {x.label}
        </a>
      ))}
    </nav>
  );
}
