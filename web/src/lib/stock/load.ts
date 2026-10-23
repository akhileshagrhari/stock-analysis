import type { Explanation, Financials, Instrument, ModelInfo, NewsItem, PriceBar, Signal } from "@/lib/api/types";

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
};
