import { describe, expect, it } from "vitest";
import { priceStats, slicePeriod, toCloses, yearRange } from "@/lib/prices";
import { fixtureStock } from "../fixtures";

describe("prices", () => {
  const c = toCloses(fixtureStock("RELIANCE").prices);

  it("keeps date and close, rounded to paise", () => {
    expect(c.at(-1)).toEqual(["2026-08-14", 1310]);
    expect(c.every(([, v]) => Number(v.toFixed(2)) === v)).toBe(true);
    expect(c[0][1]).toBe(975.52);
  });

  it("drops bars with no close", () => {
    expect(toCloses([{ ...fixtureStock("RELIANCE").prices[0], close: null }])).toEqual([]);
  });

  it("slices from the same calendar day N months back (TZ=Asia/Kolkata)", () => {
    expect(slicePeriod(c, "1Y")[0]).toEqual(["2025-08-14", 1373.8]);
    expect(slicePeriod(c, "MAX")).toHaveLength(c.length);
    expect(slicePeriod(c, "1M")[0][0] >= "2026-07-14").toBe(true);
  });

  it("clamps month-end when slicing back", () => {
    const pts: [string, number][] = [["2026-02-27", 1], ["2026-03-02", 2], ["2026-03-31", 3]];
    expect(slicePeriod(pts, "1M")[0][0]).toBe("2026-03-02");
  });

  it("computes the change over a period", () => {
    const st = priceStats(c, "1Y")!;
    expect(st.change.toFixed(2)).toBe("-63.80");
    expect((st.changePct * 100).toFixed(2)).toBe("-4.64");
    expect(st.last).toEqual(["2026-08-14", 1310]);
  });

  it("takes the 52-week high and low from intraday highs and lows, as NSE prints them", () => {
    // The closing range is 1,258.80-1,592.30; intraday it is 1,249.80-1,611.80.
    expect(yearRange(fixtureStock("RELIANCE").prices)).toEqual({ high: 1611.8, low: 1249.8 });
  });

  it("falls back to the close for a bar without a high or low", () => {
    const bar = { ...fixtureStock("RELIANCE").prices.at(-1)!, high: null, low: null, close: 2000 };
    expect(yearRange([bar])).toEqual({ high: 2000, low: 2000 });
    expect(yearRange([])).toBeNull();
  });

  it("is null for an empty history (Review Focus 3)", () => {
    expect(priceStats([], "1Y")).toBeNull();
  });
});
