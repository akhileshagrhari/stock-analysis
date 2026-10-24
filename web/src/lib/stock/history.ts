import type { Signal } from "@/lib/api/types";
import { scoreDelta } from "@/lib/format";
import { isPartialRun, type Runs } from "@/lib/runs";
import { isSignalKey, type SignalKey } from "@/config/signals";

export type Snapshot = {
  date: string;
  score: number | null;
  signal: SignalKey | null;
  coverage: number | null;
  model: string;
  redFlags: string[];
  unknownFlags: string[];
  /** Companies scored that day, when known. */
  runSize: number | null;
  /** Scored in a run that covered under half the universe. */
  partial: boolean;
};

/** One snapshot per scored date, oldest first, marked partial when `runs` says so. */
export function snapshots(rows: Signal[], runs?: Runs): Snapshot[] {
  return rows
    .map((r) => {
      const runSize = runs?.counts[r.as_of] ?? null;
      return {
        date: r.as_of,
        score: r.composite_score,
        signal: isSignalKey(r.signal) ? r.signal : null,
        coverage: r.coverage,
        model: r.model_version ?? "",
        redFlags: r.red_flags,
        unknownFlags: r.unknown_flags,
        runSize,
        partial: runs !== undefined && runSize !== null && isPartialRun(runSize, runs.instruments),
      };
    })
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
  return first === null || last === null ? null : scoreDelta(first, last);
}

export function compare(
  then: Snapshot,
  now: Snapshot,
): { scoreDelta: number | null; signalChanged: boolean; modelChanged: boolean; comparable: boolean } {
  const modelChanged = then.model !== now.model;
  return {
    scoreDelta: then.score === null || now.score === null ? null : scoreDelta(then.score, now.score),
    signalChanged: then.signal !== now.signal,
    modelChanged,
    comparable: !modelChanged && !then.partial && !now.partial,
  };
}
