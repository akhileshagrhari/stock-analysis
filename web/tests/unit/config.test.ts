import { describe, expect, it } from "vitest";
import { FACTOR_UNITS, formatFactorRaw } from "@/config/factors";
import { flagStates, RED_FLAGS } from "@/config/redflags";
import { SIGNALS, signalLabel } from "@/config/signals";
import { BRAND, LOW_COVERAGE, STALE_AFTER_DAYS, UNIVERSE } from "@/config/site";

describe("factor raw values", () => {
  it("formats each unit", () => {
    expect(formatFactorRaw("book_to_price", 1.0445)).toBe("1.04×");
    expect(formatFactorRaw("earnings_yield", 0.0785)).toBe("7.9%");
    expect(formatFactorRaw("momentum_12_1", -0.0518)).toBe("−5.2%");
    expect(formatFactorRaw("price_to_200dma", 0.9365)).toBe("−6.4% vs trend");
    expect(formatFactorRaw("price_to_200dma", 1.2463)).toBe("+24.6% vs trend");
    expect(formatFactorRaw("margin_trend", 0.0014)).toBe("+0.14 pp");
    expect(formatFactorRaw("news_sentiment_30d", 0.4213)).toBe("+0.42");
    expect(formatFactorRaw("mystery", 1.23456)).toBe("1.23");
    expect(formatFactorRaw("roe", null)).toBe("—");
  });

  it("shows inverse PEG to four decimals, since it is a product of two fractions", () => {
    expect(formatFactorRaw("peg_inverse", 0.0034)).toBe("0.0034");
  });

  it("covers every factor the model has", () => {
    expect(Object.keys(FACTOR_UNITS).sort()).toEqual(
      [
        "accruals", "book_to_price", "cfo_to_pat", "debt_to_equity", "earnings_yield", "ebitda_to_ev",
        "fcf_yield", "interest_coverage", "margin_trend", "momentum_12_1", "news_sentiment_30d",
        "pat_cagr_3y", "peg_inverse", "price_to_200dma", "quarterly_pat_yoy", "quarterly_revenue_yoy",
        "relative_strength_6m", "revenue_cagr_3y", "roce", "roe",
      ].sort(),
    );
  });
});

describe("signals", () => {
  it("has the three labels in one place", () => {
    expect(Object.keys(SIGNALS)).toEqual(["BUY", "HOLD", "SELL"]);
    expect(SIGNALS.BUY.tone).toBe("pos");
    expect(SIGNALS.HOLD.tone).toBe("hold");
    expect(SIGNALS.SELL.tone).toBe("neg");
  });

  it("labels a null signal as Not scored", () => {
    expect(signalLabel(null)).toBe("Not scored");
    expect(signalLabel("BUY")).toBe(SIGNALS.BUY.label);
  });
});

describe("red flags", () => {
  it("lists the model's six flags in order", () => {
    expect(RED_FLAGS.map((f) => f.name)).toEqual([
      "promoter_pledge", "auditor_qualification", "weak_cash_conversion",
      "promoter_selling", "contingent_liabilities", "rating_downgrade",
    ]);
    expect(RED_FLAGS.filter((f) => !f.reachable).map((f) => f.name)).toEqual(["promoter_pledge", "rating_downgrade"]);
  });

  it("is clear only when neither triggered nor unknown", () => {
    const rel = flagStates([], ["promoter_pledge", "weak_cash_conversion", "contingent_liabilities", "rating_downgrade"]);
    expect(rel.filter((f) => f.state === "clear").map((f) => f.name)).toEqual(["auditor_qualification", "promoter_selling"]);
    expect(rel.filter((f) => f.state === "unknown")).toHaveLength(4);
  });

  it("marks triggered flags and prefers the API description", () => {
    const s = flagStates([{ name: "promoter_selling", description: "Promoter holding falling 3 consecutive quarters" }], []);
    expect(s.find((f) => f.name === "promoter_selling")).toMatchObject({ state: "triggered", label: "Promoter holding falling 3 consecutive quarters" });
  });

  it("accepts the bare names a signal row carries", () => {
    expect(flagStates(["auditor_qualification"], []).find((f) => f.name === "auditor_qualification")!.state).toBe("triggered");
  });

  it("keeps a flag the catalogue doesn't know, rather than dropping it", () => {
    const s = flagStates(["new_flag"], ["other_new"]);
    expect(s.find((f) => f.name === "new_flag")!.state).toBe("triggered");
    expect(s.find((f) => f.name === "other_new")!.state).toBe("unknown");
  });
});

describe("site constants", () => {
  it("has one brand and the agreed thresholds", () => {
    expect(BRAND).toBe("StockAnalysis");
    expect(STALE_AFTER_DAYS).toBe(7);
    expect(LOW_COVERAGE).toBe(0.5);
    expect(UNIVERSE).toBe("NIFTY100");
  });
});
