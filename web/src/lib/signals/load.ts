import { ApiUnavailableError, serverGet, type Getter } from "@/lib/api/client";
import type { Health, ModelInfo, Signal } from "@/lib/api/types";
import { isSignalKey, type SignalKey } from "@/config/signals";
import { isPartialRun, signalsOn } from "@/lib/runs";
import { counts, partialNotice, sortRows, withPrevious, type Row } from "./list";

const MAX_LOOKBACK = 5;

type Fuller = { date: string; count: number } | null;

async function required<T>(get: Getter, path: string): Promise<T> {
  const v = await get<T>(path);
  if (v === null) throw new ApiUnavailableError(404, path);
  return v;
}

/**
 * The newest date older than `dates[from]` that scored at least half the
 * universe, looking back at most MAX_LOOKBACK dates. `seen` holds rows
 * already fetched, so the previous date isn't fetched twice.
 */
async function findFuller(get: Getter, dates: string[], from: number, instruments: number, seen: Map<string, Signal[]>): Promise<Fuller> {
  for (const date of dates.slice(from + 1, from + 1 + MAX_LOOKBACK)) {
    const rows = seen.get(date) ?? (await signalsOn(get, date)) ?? [];
    seen.set(date, rows);
    if (!isPartialRun(rows.length, instruments)) return { date, count: rows.length };
  }
  return null;
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

export type RedFlagsData = { flagged: Signal[]; asOf: string | null };

/** Companies with a triggered red flag on the API's latest date. */
export async function loadRedFlags(get: Getter = serverGet): Promise<RedFlagsData> {
  const [health, flagged] = await Promise.all([required<Health>(get, "/health"), required<Signal[]>(get, "/signals/red-flags?limit=1000")]);
  return { flagged, asOf: health.latest_as_of ?? null };
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
