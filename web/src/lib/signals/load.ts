import { ApiUnavailableError, serverGet, type Getter } from "@/lib/api/client";
import type { Health, ModelInfo, Signal } from "@/lib/api/types";
import { isSignalKey, type SignalKey } from "@/config/signals";
import { counts, partialNotice, sortRows, withPrevious, type Row } from "./list";

const MAX_LOOKBACK = 5;

type Fuller = { date: string; count: number } | null;

async function required<T>(get: Getter, path: string): Promise<T> {
  const v = await get<T>(path);
  if (v === null) throw new ApiUnavailableError(404, path);
  return v;
}

function signalsOn(get: Getter, date: string): Promise<Signal[] | null> {
  return get<Signal[]>(`/signals?as_of=${date}&limit=1000`);
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
    if (rows.length >= 0.5 * instruments) return { date, count: rows.length };
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
  const all = withPrevious(current ?? [], previous ?? []);
  const seen = new Map<string, Signal[]>();
  if (previousAsOf) seen.set(previousAsOf, previous ?? []);
  const partial = all.length < 0.5 * health.instruments;
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

export type HomeData = {
  asOf: string;
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
  const [rows, flagged] = await Promise.all([signalsOn(get, asOf), get<Signal[]>(`/signals/red-flags?as_of=${asOf}&limit=1000`)]);
  const all = rows ?? [];
  const scored = all.filter((r) => r.composite_score !== null);
  const partial = all.length < 0.5 * health.instruments;
  const fuller = partial ? await findFuller(get, dates, 0, health.instruments, new Map()) : null;

  // Top and bottom never overlap: with fewer than ten scored, split them.
  const ranked = sortRows(withPrevious(scored, []), "score", "desc");
  const nTop = Math.min(5, Math.ceil(ranked.length / 2));
  const nBottom = Math.min(5, ranked.length - nTop);

  return {
    asOf,
    counts: counts(all),
    scored: scored.length,
    notScored: Math.max(0, health.instruments - scored.length),
    instruments: health.instruments,
    top: ranked.slice(0, nTop),
    bottom: ranked.slice(ranked.length - nBottom).reverse(),
    flagged: flagged ?? [],
    model,
    fuller,
    notice: partialNotice(asOf, all.length, health.instruments, fuller),
  };
}
