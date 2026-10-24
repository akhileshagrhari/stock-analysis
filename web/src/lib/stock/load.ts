import { ApiUnavailableError, serverGet, type Getter } from "@/lib/api/client";
import type { Explanation, Financials, Health, Instrument, ModelInfo, NewsItem, PriceBar, Signal } from "@/lib/api/types";
import { loadRuns, type Runs } from "@/lib/runs";

/** Everything the stock page renders, fetched once on the server. */
export type StockPageData = {
  instrument: Instrument;
  explanation: Explanation | null;
  history: Signal[];
  prices: PriceBar[];
  financials: Financials;
  news: NewsItem[];
  model: ModelInfo;
  peers: Signal[];
  peersAsOf: string | null;
  /** Companies scored on each of this company's dates (and the peers' date). */
  runs: Runs;
};

const EMPTY_FINANCIALS: Financials = { line_items: [], annual: [], quarterly: [] };

/**
 * Resolve a symbol (any case, URL-special characters allowed) and load the
 * page's data in parallel. Null when no company has that symbol; an outage
 * throws ApiUnavailableError.
 */
export async function loadStockPage(symbol: string, get: Getter = serverGet): Promise<StockPageData | null> {
  const instrument = await get<Instrument>(`/instruments/by-symbol/${encodeURIComponent(symbol.toUpperCase())}`);
  if (instrument === null) return null;

  const base = `/instruments/${encodeURIComponent(instrument.isin)}`;
  const [explanation, history, prices, financials, news, model, health] = await Promise.all([
    get<Explanation>(`${base}/explanation`),
    get<Signal[]>(`${base}/history?limit=500`),
    // Full stored range (~1,500 bars); the client chart slices by period.
    get<PriceBar[]>(`${base}/prices?from=2000-01-01`),
    get<Financials>(`${base}/financials?quarters=12`),
    get<NewsItem[]>(`${base}/news?limit=20`),
    get<ModelInfo>("/model"),
    get<Health>("/health"),
  ]);
  if (model === null) throw new ApiUnavailableError(404, "/model");

  // Peers are the same sector on the same date. An unscored company has no
  // date of its own, so it gets the latest scored date.
  const peersAsOf = explanation?.as_of ?? model.scored_dates[0] ?? null;
  const [peers, runs] = await Promise.all([
    peersAsOf && instrument.sector
      ? get<Signal[]>(`/signals?sector=${encodeURIComponent(instrument.sector)}&as_of=${peersAsOf}&limit=1000`)
      : [],
    loadRuns(get, [...(history ?? []).map((h) => h.as_of), ...(peersAsOf ? [peersAsOf] : [])], health?.instruments ?? 0),
  ]);

  return {
    instrument,
    explanation,
    history: history ?? [],
    prices: prices ?? [],
    financials: financials ?? EMPTY_FINANCIALS,
    news: news ?? [],
    model,
    peers: peers ?? [],
    peersAsOf,
    runs,
  };
}
