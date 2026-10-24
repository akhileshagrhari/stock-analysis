import type { Family } from "@/lib/api/types";
import { pct } from "@/lib/format";

// Coverage is the share of the model's weight backed by data, weighted by
// family and by each factor's weight within its family (the API's own
// description of `coverage`). It is not a share of factors, so pages say
// "of the model's weight" and give the factor count separately.

/** "Data behind 90% of the model's weight". */
export function coverageText(coverage: number): string {
  return `Data behind ${pct(coverage, 0)} of the model's weight`;
}

/** Short form for tooltips: "data behind 90% of weight". */
export function coverageShort(coverage: number | null): string {
  return coverage === null ? "coverage unknown" : `data behind ${pct(coverage, 0)} of weight`;
}

/** Factors measured out of all the model's factors, summed over families. */
export function factorCount(families: Family[]): { measured: number; total: number } {
  let measured = 0;
  let total = 0;
  for (const f of families) {
    measured += f.factors_measured;
    total += f.factors_total;
  }
  return { measured, total };
}
