import type { Explanation, Family, ModelInfo, NewsMix } from "@/lib/api/types";
import { roundTo, scoreValue } from "@/lib/format";
import { LOW_COVERAGE } from "@/config/site";
import { RED_FLAGS } from "@/config/redflags";
import { isSignalKey, SIGNALS, type SignalKey } from "@/config/signals";

export type ScoreState = {
  kind: "scored" | "unscored" | "below_floor";
  score: number | null;
  signal: SignalKey | null;
  asOf: string | null;
  coverage: number | null;
  lowCoverage: boolean;
  /** The band the score itself falls in, before any red-flag overlay. */
  band: SignalKey | null;
  overridden: { description: string; wouldBe: SignalKey } | null;
  stale: { storedVersion: string } | null;
};

export function bandOf(score: number, model: ModelInfo): SignalKey {
  if (score >= model.buy_threshold) return "BUY";
  if (score < model.sell_threshold) return "SELL";
  return "HOLD";
}

export function scoreState(expl: Explanation | null, model: ModelInfo): ScoreState {
  if (expl === null) {
    return {
      kind: "unscored", score: null, signal: null, asOf: null, coverage: null,
      lowCoverage: false, band: null, overridden: null, stale: null,
    };
  }
  const coverage = expl.coverage;
  const lowCoverage = coverage !== null && coverage < LOW_COVERAGE;
  const stale = expl.stale ? { storedVersion: expl.stored_version ?? "an earlier model" } : null;
  const score = expl.composite_score;
  if (score === null || !Number.isFinite(score)) {
    return {
      kind: "below_floor", score: null, signal: null, asOf: expl.as_of, coverage,
      lowCoverage, band: null, overridden: null, stale,
    };
  }
  const signal = isSignalKey(expl.signal) ? expl.signal : null;
  const band = bandOf(score, model);
  let overridden: ScoreState["overridden"] = null;
  if (signal === "SELL" && band !== "SELL" && expl.red_flags.length > 0) {
    const description = expl.red_flags
      .map((f) => f.description ?? RED_FLAGS.find((d) => d.name === f.name)?.description ?? f.name)
      .join("; ");
    overridden = { description, wouldBe: band };
  }
  return { kind: "scored", score, signal, asOf: expl.as_of, coverage, lowCoverage, band, overridden, stale };
}

/** How far the displayed score is from the next band line. */
export function bandGap(score: number, model: ModelInfo): string {
  const shown = scoreValue(score);
  const gap = (line: number) => roundTo(Math.abs(line - shown), 1).toFixed(1);
  if (score < model.sell_threshold) return `${gap(model.sell_threshold)} pts to ${SIGNALS.HOLD.label}`;
  if (score < model.buy_threshold) return `${gap(model.buy_threshold)} pts to ${SIGNALS.BUY.label}`;
  return `${gap(model.buy_threshold)} pts above the ${SIGNALS.BUY.label} line`;
}

const MOVES = 0.05;

function joinNames(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

function capitalise(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** One line on which families push the score which way. */
export function whySummary(families: Family[]): string {
  const byWeight = (a: Family, b: Family) => Math.abs(b.contribution!) - Math.abs(a.contribution!);
  const up = families.filter((f) => f.contribution !== null && f.contribution > MOVES).sort(byWeight).map((f) => f.family);
  const down = families.filter((f) => f.contribution !== null && f.contribution < -MOVES).sort(byWeight).map((f) => f.family);
  const verb = (n: string[]) => (n.length === 1 ? "pulls" : "pull");
  if (up.length && down.length) {
    return `${capitalise(joinNames(up))} ${verb(up)} the score up; ${joinNames(down)} ${verb(down)} it down`;
  }
  if (up.length) return `${capitalise(joinNames(up))} ${verb(up)} the score up`;
  if (down.length) return `${capitalise(joinNames(down))} ${verb(down)} the score down`;
  return "No family moves the score much";
}

export type NewsTone = { label: "Positive" | "Mixed" | "Negative" | null; needle: number | null; counts: string };

export function newsTone(mix: NewsMix | null): NewsTone {
  if (mix === null || mix.total <= 0) return { label: null, needle: null, counts: "No scored news in 30 days" };
  const needle = ((mix.positive - mix.negative) / mix.total + 1) / 2;
  const label = needle >= 0.6 ? "Positive" : needle <= 0.4 ? "Negative" : "Mixed";
  return { label, needle, counts: `${mix.positive} positive · ${mix.negative} negative · ${mix.neutral} neutral` };
}

export function unscoredMessage(sector: string | null): string {
  const base = "Our model hasn't scored this company.";
  if (sector === "Financial Services") {
    return `${base} The filings we read for banks and NBFCs don't carry several figures the model needs.`;
  }
  return base;
}
