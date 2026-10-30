// Typed loaders for the real API responses captured by
// scripts/capture-fixtures.mjs. Tests only.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Health, ModelInfo, Portfolio, Signal } from "@/lib/api/types";
import type { StockPageData } from "@/lib/stock/load";

// Vitest runs from web/; under jsdom import.meta.url is not a file: URL.
const dir = join(process.cwd(), "tests", "fixtures");

function read<T>(file: string): T {
  return JSON.parse(readFileSync(join(dir, file), "utf8")) as T;
}

export type FixtureSymbol = "RELIANCE" | "ABB" | "HDFCBANK" | "CHOLAFIN" | "ADANIENSOL";

export function fixtureModel(): ModelInfo {
  return read("model.json");
}

export function fixtureHealth(): Health {
  return read("health.json");
}

/**
 * GET /v1/me/portfolio on the dev data (30 Oct 2026) for a test user who
 * imported tests/fixtures/portfolio/kite_holdings.csv with Replace.
 */
export function fixturePortfolio(): Portfolio {
  return read("portfolio.json");
}

export function fixtureSectors(): string[] {
  return read("sectors.json");
}

export function fixtureRedFlags(): Signal[] {
  return read("red-flags.json");
}

export function fixtureSignals(date: "2026-08-14" | "2026-08-15" | "2026-08-16"): Signal[] {
  return read(`signals-${date}.json`);
}

/**
 * Companies scored per date, from the signals fixtures. 31 Jul isn't
 * captured; the API reported 77 for it (24 Oct 2026).
 */
export function fixtureRuns(): StockPageData["runs"] {
  return {
    instruments: fixtureHealth().instruments,
    counts: {
      "2026-07-31": 77,
      "2026-08-14": fixtureSignals("2026-08-14").length,
      "2026-08-15": fixtureSignals("2026-08-15").length,
      "2026-08-16": fixtureSignals("2026-08-16").length,
    },
  };
}

export function fixtureStock(symbol: FixtureSymbol): StockPageData {
  const model = fixtureModel();
  const explanation = read<StockPageData["explanation"]>(`${symbol}/explanation.json`);
  const instrument = read<StockPageData["instrument"]>(`${symbol}/instrument.json`);
  // As the loader does: an unscored company's peers come from the latest
  // full run (14 Aug), not the partial 16 Aug run the peers.json was captured on.
  const peersAsOf = explanation?.as_of ?? "2026-08-14";
  const peers = explanation ? read<Signal[]>(`${symbol}/peers.json`) : fixtureSignals("2026-08-14").filter((r) => r.sector === instrument.sector);
  return {
    instrument,
    explanation,
    history: read(`${symbol}/history.json`),
    prices: read(`${symbol}/prices.json`),
    financials: read(`${symbol}/financials.json`),
    news: read(`${symbol}/news.json`),
    model,
    peers,
    peersAsOf,
    runs: fixtureRuns(),
  };
}
