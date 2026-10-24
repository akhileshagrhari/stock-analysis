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

export type PriceStats = { last: Close; change: number; changePct: number };

/** Last close and the change over the chosen period. */
export function priceStats(c: Close[], p: Period): PriceStats | null {
  if (c.length === 0) return null;
  const period = slicePeriod(c, p);
  const last = c[c.length - 1];
  const first = period[0] ?? last;
  return {
    last,
    change: last[1] - first[1],
    changePct: first[1] > 0 ? last[1] / first[1] - 1 : 0,
  };
}

export type Range = { high: number; low: number };

/**
 * The 52-week high and low as exchanges print them: the highest intraday
 * high and lowest intraday low over the last year (the close stands in for a
 * bar without them). Same window as the 1Y period.
 */
export function yearRange(bars: PriceBar[]): Range | null {
  const usable = bars.filter((b) => b.close !== null && Number.isFinite(b.close));
  if (usable.length === 0) return null;
  const from = monthsBefore(usable[usable.length - 1].date, MONTHS_BACK["1Y"]);
  let high = -Infinity;
  let low = Infinity;
  for (const b of usable) {
    if (b.date < from) continue;
    high = Math.max(high, b.high ?? b.close!);
    low = Math.min(low, b.low ?? b.close!);
  }
  return { high: Math.round(high * 100) / 100, low: Math.round(low * 100) / 100 };
}
