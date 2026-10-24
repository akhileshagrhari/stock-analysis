import { ApiUnavailableError, serverGet, type Getter } from "@/lib/api/client";
import type { Health, ModelInfo, Signal } from "@/lib/api/types";
import { isSignalKey, type SignalKey } from "@/config/signals";
import { firstFullRun, isPartialRun, signalsOn, type FullRun } from "@/lib/runs";
import { counts, partialNotice, sortRows, withPrevious, type Row } from "./list";

type Fuller = FullRun;

async function required<T>(get: Getter, path: string): Promise<T> {
  const v = await get<T>(path);
  if (v === null) throw new ApiUnavailableError(404, path);
  return v;
}

/** The newest full run older than `dates[from]`; `seen` saves refetching rows. */
function findFuller(get: Getter, dates: string[], from: number, instruments: number, seen: Map<string, Signal[]>): Promise<Fuller> {
  return firstFullRun(get, dates.slice(from + 1), instruments, seen);
}

export type SignalsData = {
  rows: Row[];
  total: number;
  asOf: string;
  previousAsOf: string | null;
  dates: string[];
  sectors: string[];
  instruments: number;
  fuller: Fuller;
  notice: string | null;
  filters: { signal: SignalKey | null; sector: string | null };
};

export async function loadSignals(q: { signal?: string; sector?: string; as_of?: string }, get: Getter = serverGet): Promise<SignalsData> {
  const [model, health, sectors] = await Promise.all([
    required<ModelInfo>(get, "/model"),
    required<Health>(get, "/health"),
    get<string[]>("/sectors"),
  ]);
  const dates = model.scored_dates;
  const asOf = q.as_of && dates.includes(q.as_of) ? q.as_of : dates[0];
  const i = dates.indexOf(asOf);
  const previousAsOf = dates[i + 1] ?? null;

  const [current, previous] = await Promise.all([signalsOn(get, asOf), previousAsOf ? signalsOn(get, previousAsOf) : null]);
  const partial = isPartialRun((current ?? []).length, health.instruments);
  const previousPartial = previous !== null && isPartialRun((previous ?? []).length, health.instruments);
  const all = withPrevious(current ?? [], previous ?? [], partial || previousPartial);
  const seen = new Map<string, Signal[]>();
  if (previousAsOf) seen.set(previousAsOf, previous ?? []);
  const fuller = partial ? await findFuller(get, dates, i, health.instruments, seen) : null;

  const signal = isSignalKey(q.signal) ? q.signal : null;
  const sector = q.sector && (sectors ?? []).includes(q.sector) ? q.sector : null;
  const rows = all.filter((r) => (signal === null || r.signal === signal) && (sector === null || r.sector === sector));

  return {
    rows,
    total: all.length,
    asOf,
    previousAsOf,
    dates,
    sectors: sectors ?? [],
    instruments: health.instruments,
    fuller,
    notice: partialNotice(asOf, all.length, health.instruments, fuller),
    filters: { signal, sector },
  };
}

export type RedFlagsData = { flagged: Signal[]; asOf: string | null; notice: string | null; fuller: Fuller };

/** Companies with a triggered red flag on a scored date (the latest by default), with the partial-run notice. */
export async function loadRedFlags(q: { as_of?: string }, get: Getter = serverGet): Promise<RedFlagsData> {
  const [model, health] = await Promise.all([required<ModelInfo>(get, "/model"), required<Health>(get, "/health")]);
  const dates = model.scored_dates;
  const asOf = q.as_of && dates.includes(q.as_of) ? q.as_of : (dates[0] ?? null);
  if (asOf === null) return { flagged: [], asOf: null, notice: null, fuller: null };
  const [flagged, rows] = await Promise.all([required<Signal[]>(get, `/signals/red-flags?as_of=${asOf}&limit=1000`), signalsOn(get, asOf)]);
  const count = (rows ?? []).length;
  const fuller = isPartialRun(count, health.instruments) ? await findFuller(get, dates, dates.indexOf(asOf), health.instruments, new Map()) : null;
  return { flagged, asOf, notice: partialNotice(asOf, count, health.instruments, fuller), fuller };
}

export type HomeData = {
  asOf: string;
  /** The date the top, bottom and red-flag lists use: the latest fuller run when the latest is partial. */
  listsAsOf: string;
  counts: Record<SignalKey, number>;
  scored: number;
  notScored: number;
  instruments: number;
  top: Signal[];
  bottom: Signal[];
  flagged: Signal[];
  model: ModelInfo;
  fuller: Fuller;
  notice: string | null;
};

export async function loadHome(get: Getter = serverGet): Promise<HomeData> {
  const [model, health] = await Promise.all([required<ModelInfo>(get, "/model"), required<Health>(get, "/health")]);
  const dates = model.scored_dates;
  const asOf = dates[0];
  const all = (await signalsOn(get, asOf)) ?? [];
  const scored = all.filter((r) => r.composite_score !== null);
  const seen = new Map<string, Signal[]>();
  const fuller = isPartialRun(all.length, health.instruments) ? await findFuller(get, dates, 0, health.instruments, seen) : null;
  // A partial run's scores are ranks among a few companies; list the fuller run instead.
  const listsAsOf = fuller?.date ?? asOf;
  const listRows = fuller ? (seen.get(fuller.date) ?? []) : all;

  // Top and bottom never overlap: with fewer than ten scored, split them.
  const ranked = sortRows(withPrevious(listRows.filter((r) => r.composite_score !== null), []), "score", "desc");
  const nTop = Math.min(5, Math.ceil(ranked.length / 2));
  const nBottom = Math.min(5, ranked.length - nTop);

  return {
    asOf,
    listsAsOf,
    counts: counts(all),
    scored: scored.length,
    notScored: Math.max(0, health.instruments - scored.length),
    instruments: health.instruments,
    top: ranked.slice(0, nTop),
    bottom: ranked.slice(ranked.length - nBottom).reverse(),
    flagged: listRows.filter((r) => r.red_flags.length > 0),
    model,
    fuller,
    notice: partialNotice(asOf, all.length, health.instruments, fuller),
  };
}
