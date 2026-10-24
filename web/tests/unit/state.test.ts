import { describe, expect, it } from "vitest";
import { bandGap, newsTone, scoreState, unscoredMessage, whySummary } from "@/lib/stock/state";
import { fixtureModel, fixtureStock } from "../fixtures";

const m = fixtureModel();

describe("scoreState", () => {
  it("reads an ordinary scored company", () => {
    const rel = scoreState(fixtureStock("RELIANCE").explanation, m);
    expect(rel).toMatchObject({ kind: "scored", signal: "SELL", band: "SELL", overridden: null, lowCoverage: false, stale: null, asOf: "2026-08-16" });
    expect(rel.score).toBeCloseTo(28.435, 3);
  });

  it("is unscored when there is no explanation", () => {
    const s = scoreState(null, m);
    expect(s).toMatchObject({ kind: "unscored", score: null, signal: null, band: null, asOf: null, lowCoverage: false });
  });

  it("spots a red-flag override, low coverage and a stale breakdown", () => {
    const chola = scoreState(fixtureStock("CHOLAFIN").explanation, m);
    expect(chola.overridden).toEqual({ description: "Promoter holding falling 3 consecutive quarters", wouldBe: "BUY" });
    expect(chola.lowCoverage).toBe(true);
    expect(chola.stale).toEqual({ storedVersion: expect.stringContaining("a8756fe2") });
  });

  it("does not call it an override when the score was SELL anyway", () => {
    expect(scoreState(fixtureStock("ADANIENSOL").explanation, m).overridden).toBeNull();
  });

  it("is below the floor when a row exists with no score (Review Focus 5)", () => {
    const s = scoreState({ ...fixtureStock("RELIANCE").explanation!, composite_score: null, signal: null }, m);
    expect(s).toMatchObject({ kind: "below_floor", score: null, signal: null, band: null, asOf: "2026-08-16" });
  });
});

describe("bandGap", () => {
  it("measures the distance to the next band", () => {
    const rel = scoreState(fixtureStock("RELIANCE").explanation, m);
    expect(bandGap(rel.score!, m)).toBe("16.6 pts to HOLD");
    expect(bandGap(60, m)).toBe("15.0 pts to BUY");
    expect(bandGap(74.96, m)).toBe("0.1 pts to BUY");
    expect(bandGap(93.29, m)).toBe("18.3 pts above the BUY line");
  });
});

describe("whySummary", () => {
  it("names the families that move the score", () => {
    expect(whySummary(fixtureStock("RELIANCE").explanation!.families)).toBe("Value pulls the score up; quality and momentum pull it down");
    expect(whySummary(fixtureStock("ABB").explanation!.families)).toBe("Quality, momentum and growth pull the score up; value pulls it down");
  });

  it("handles one-sided and flat cases", () => {
    const fam = (family: string, contribution: number | null) => ({ ...fixtureStock("ABB").explanation!.families[0], family, contribution });
    expect(whySummary([fam("growth", 0.2), fam("momentum", 0.6)])).toBe("Momentum and growth pull the score up");
    expect(whySummary([fam("value", -0.3)])).toBe("Value pulls the score down");
    expect(whySummary([fam("value", 0.01), fam("quality", null)])).toBe("No family moves the score much");
  });
});

describe("newsTone", () => {
  it("has no needle without scored news", () => {
    expect(newsTone(null)).toEqual({ label: null, needle: null, counts: "No scored news in 30 days" });
  });

  it("places the needle by the positive-negative balance", () => {
    expect(newsTone({ positive: 1, negative: 1, neutral: 1, total: 3 })).toMatchObject({ label: "Mixed", needle: 0.5, counts: "1 positive · 1 negative · 1 neutral" });
    expect(newsTone({ positive: 3, negative: 0, neutral: 1, total: 4 }).label).toBe("Positive");
    expect(newsTone({ positive: 0, negative: 2, neutral: 0, total: 2 })).toMatchObject({ label: "Negative", needle: 0 });
  });
});

describe("unscoredMessage", () => {
  it("explains the bank gap for financial companies only", () => {
    expect(unscoredMessage("Financial Services")).toContain("banks and NBFCs");
    expect(unscoredMessage("Construction Materials")).toBe("Our model hasn't scored this company.");
    expect(unscoredMessage(null)).toBe("Our model hasn't scored this company.");
  });
});
