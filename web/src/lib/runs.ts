import type { Getter } from "@/lib/api/client";
import type { Signal } from "@/lib/api/types";

// A scoring run that covered fewer than half the universe is "partial":
// its scores are ranks among the few companies scored that day (16 Aug 2026
// scored 4), so they are not comparable with a full run's.

export const PARTIAL_SHARE = 0.5;

/** Companies scored on each date, and the universe they are out of. */
export type Runs = { instruments: number; counts: Record<string, number> };

export function isPartialRun(count: number, instruments: number): boolean {
  return instruments > 0 && count < PARTIAL_SHARE * instruments;
}

/** "1 company", "4 companies". */
export function companies(n: number): string {
  return `${n} ${n === 1 ? "company" : "companies"}`;
}

/** Every signal row on one date (the same URL everywhere, so the fetch cache shares it). */
export function signalsOn(get: Getter, date: string): Promise<Signal[] | null> {
  return get<Signal[]>(`/signals?as_of=${date}&limit=1000`);
}

/** How many companies each date's run scored. */
export async function loadRuns(get: Getter, dates: string[], instruments: number): Promise<Runs> {
  const unique = [...new Set(dates)];
  const rows = await Promise.all(unique.map((d) => signalsOn(get, d)));
  const counts: Record<string, number> = {};
  unique.forEach((d, i) => {
    const r = rows[i];
    if (r) counts[d] = r.length;
  });
  return { instruments, counts };
}

/** How many dates back to look for a run that isn't partial. */
export const MAX_LOOKBACK = 5;

export type FullRun = { date: string; count: number } | null;

/**
 * The first of `dates` (newest first) whose run scored at least half the
 * universe, looking at most MAX_LOOKBACK dates. `seen` holds rows already
 * fetched, and collects the ones fetched here.
 */
export async function firstFullRun(get: Getter, dates: string[], instruments: number, seen: Map<string, Signal[]> = new Map()): Promise<FullRun> {
  for (const date of dates.slice(0, MAX_LOOKBACK)) {
    const rows = seen.get(date) ?? (await signalsOn(get, date)) ?? [];
    seen.set(date, rows);
    if (!isPartialRun(rows.length, instruments)) return { date, count: rows.length };
  }
  return null;
}
