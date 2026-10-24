// Typed loaders for the real API responses captured by
// scripts/capture-fixtures.mjs. Tests only.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Health, ModelInfo, Signal } from "@/lib/api/types";
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
  return {
    instrument: read(`${symbol}/instrument.json`),
    explanation,
    history: read(`${symbol}/history.json`),
    prices: read(`${symbol}/prices.json`),
    financials: read(`${symbol}/financials.json`),
    news: read(`${symbol}/news.json`),
    model,
    peers: read(`${symbol}/peers.json`),
    peersAsOf: explanation?.as_of ?? model.scored_dates[0] ?? null,
    runs: fixtureRuns(),
  };
}
