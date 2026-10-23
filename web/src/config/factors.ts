import { absFixed, MINUS, pct, roundTo, signedFixed } from "@/lib/format";

// How each factor's raw value reads, checked against
// src/stockanalysis/factors/*.py. Until /v1/model carries units (R3) this map
// is the source. A factor it doesn't know falls back to two decimals.
export type FactorUnit = "percent" | "pp" | "multiple" | "vs_trend" | "score" | "ratio";

export const FACTOR_UNITS: Record<string, FactorUnit> = {
  earnings_yield: "percent",
  ebitda_to_ev: "percent",
  fcf_yield: "percent",
  roe: "percent",
  roce: "percent",
  revenue_cagr_3y: "percent",
  pat_cagr_3y: "percent",
  quarterly_revenue_yoy: "percent",
  quarterly_pat_yoy: "percent",
  momentum_12_1: "percent",
  relative_strength_6m: "percent",
  accruals: "percent",
  margin_trend: "pp",
  book_to_price: "multiple",
  debt_to_equity: "multiple",
  cfo_to_pat: "multiple",
  interest_coverage: "multiple",
  // E/P × PAT growth: a product of two fractions, typically ~0.003, so two
  // decimals would always read 0.00.
  peg_inverse: "ratio",
  price_to_200dma: "vs_trend",
  news_sentiment_30d: "score",
};

export function formatFactorRaw(name: string, raw: number | null): string {
  if (raw === null || !Number.isFinite(raw)) return "—";
  switch (FACTOR_UNITS[name]) {
    case "percent":
      return pct(raw, 1);
    case "pp":
      return `${signedFixed(raw * 100, 2)} pp`;
    case "multiple":
      return `${fixed(raw, 2)}×`;
    case "vs_trend":
      return `${pct(raw - 1, 1, true)} vs trend`;
    case "score":
      return signedFixed(raw, 2);
    case "ratio":
      return fixed(raw, 4);
    default:
      return fixed(raw, 2);
  }
}

function fixed(v: number, digits: number): string {
  const s = absFixed(v, digits);
  return roundTo(v, digits) < 0 ? MINUS + s : s;
}
