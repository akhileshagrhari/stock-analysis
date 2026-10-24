import type { Signal } from "@/lib/api/types";
import { fmtDate, scoreDelta } from "@/lib/format";
import type { SignalKey } from "@/config/signals";

/** A signal row with its change since the previous scored date. */
export type Row = Signal & { delta: number | null; modelChanged: boolean };

export function withPrevious(current: Signal[], previous: Signal[]): Row[] {
  const before = new Map(previous.map((p) => [p.isin, p]));
  return current.map((r) => {
    const p = before.get(r.isin);
    const delta = p && p.composite_score !== null && r.composite_score !== null ? scoreDelta(p.composite_score, r.composite_score) : null;
    return { ...r, delta, modelChanged: p !== undefined && p.model_version !== r.model_version };
  });
}

export type SortKey = "score" | "delta" | "coverage" | "symbol";

/** Sorted copy; a missing value (an unscored row) always goes last. */
export function sortRows(rows: Row[], key: SortKey, dir: "asc" | "desc"): Row[] {
  const sign = dir === "asc" ? 1 : -1;
  const value = (r: Row): number | string | null =>
    key === "score" ? r.composite_score : key === "delta" ? r.delta : key === "coverage" ? r.coverage : r.nse_symbol;
  return [...rows].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    return (va < vb ? -1 : va > vb ? 1 : 0) * sign;
  });
}

/** A notice when a date scored fewer than half the universe, pointing at the latest fuller date. */
export function partialNotice(asOf: string, count: number, instruments: number, fuller: { date: string; count: number } | null): string | null {
  if (instruments <= 0 || count >= 0.5 * instruments) return null;
  const head = `Only ${count} of ${instruments} companies were scored on ${fmtDate(asOf)}.`;
  return fuller ? `${head} The most recent fuller list is ${fmtDate(fuller.date)} (${fuller.count} companies).` : head;
}

export function counts(rows: Signal[]): Record<SignalKey, number> {
  const out: Record<SignalKey, number> = { BUY: 0, HOLD: 0, SELL: 0 };
  for (const r of rows) if (r.signal === "BUY" || r.signal === "HOLD" || r.signal === "SELL") out[r.signal]++;
  return out;
}
