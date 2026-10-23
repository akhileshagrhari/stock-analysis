import type { Signal } from "@/lib/api/types";
import { isSignalKey, type SignalKey } from "@/config/signals";

export type Snapshot = {
  date: string;
  score: number | null;
  signal: SignalKey | null;
  coverage: number | null;
  model: string;
  redFlags: string[];
  unknownFlags: string[];
};

/** One snapshot per scored date, oldest first. */
export function snapshots(rows: Signal[]): Snapshot[] {
  return rows
    .map((r) => ({
      date: r.as_of,
      score: r.composite_score,
      signal: isSignalKey(r.signal) ? r.signal : null,
      coverage: r.coverage,
      model: r.model_version ?? "",
      redFlags: r.red_flags,
      unknownFlags: r.unknown_flags,
    }))
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}

/** Index i means the model changed between snapshot i-1 and i. */
export function modelChangeIndexes(s: Snapshot[]): number[] {
  const out: number[] = [];
  for (let i = 1; i < s.length; i++) if (s[i].model !== s[i - 1].model) out.push(i);
  return out;
}

export function changeSinceFirst(s: Snapshot[]): number | null {
  if (s.length < 2) return null;
  const first = s[0].score;
  const last = s[s.length - 1].score;
  return first === null || last === null ? null : last - first;
}

export function compare(then: Snapshot, now: Snapshot): { scoreDelta: number | null; signalChanged: boolean; comparable: boolean } {
  return {
    scoreDelta: then.score === null || now.score === null ? null : now.score - then.score,
    signalChanged: then.signal !== now.signal,
    comparable: then.model === now.model,
  };
}
