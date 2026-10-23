import type { PriceBar } from "@/lib/api/types";
import { isoDay, parseDay } from "@/lib/format";

export type Close = [date: string, close: number];
export type Period = "1M" | "3M" | "6M" | "1Y" | "3Y" | "MAX";

export const PERIODS: Period[] = ["1M", "3M", "6M", "1Y", "3Y", "MAX"];
const MONTHS_BACK: Record<Exclude<Period, "MAX">, number> = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36 };

/** Date and close only, rounded to paise; the client chart gets this. */
export function toCloses(bars: PriceBar[]): Close[] {
  const out: Close[] = [];
  for (const b of bars) {
    if (b.close === null || !Number.isFinite(b.close)) continue;
    out.push([b.date, Math.round(b.close * 100) / 100]);
  }
  return out;
}

function monthsBefore(iso: string, months: number): string {
  const d = parseDay(iso);
  const target = new Date(d.getFullYear(), d.getMonth() - months, 1);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  target.setDate(Math.min(d.getDate(), lastDay));
  return isoDay(target);
}

/** Bars from the first one on or after the same day N months before the last. */
export function slicePeriod(c: Close[], p: Period): Close[] {
  if (p === "MAX" || c.length === 0) return c;
  const from = monthsBefore(c[c.length - 1][0], MONTHS_BACK[p]);
  const i = c.findIndex(([d]) => d >= from);
  return i < 0 ? [] : c.slice(i);
}

export type PriceStats = { last: Close; change: number; changePct: number; high52: number; low52: number; offHighPct: number };

export function priceStats(c: Close[], p: Period): PriceStats | null {
  if (c.length === 0) return null;
  const period = slicePeriod(c, p);
  const year = slicePeriod(c, "1Y");
  const last = c[c.length - 1];
  const first = period[0] ?? last;
  const closes = year.map(([, v]) => v);
  const high52 = Math.max(...closes);
  const low52 = Math.min(...closes);
  return {
    last,
    change: last[1] - first[1],
    changePct: first[1] > 0 ? last[1] / first[1] - 1 : 0,
    high52,
    low52,
    offHighPct: high52 > 0 ? last[1] / high52 - 1 : 0,
  };
}
