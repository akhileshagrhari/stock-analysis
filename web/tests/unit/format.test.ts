import { describe, expect, it } from "vitest";
import {
  daysBetween,
  fiscalLabel,
  fmtDate,
  fmtDay,
  inr,
  num,
  ordinal,
  parseDay,
  pct,
  quarterLabel,
  shortModel,
  zLabel,
} from "@/lib/format";

describe("dates", () => {
  it("formats a day as 16 Aug 2026", () => {
    expect(fmtDate("2026-08-16")).toBe("16 Aug 2026");
    expect(fmtDay("2026-08-16")).toBe("16 Aug");
  });

  it("parses YYYY-MM-DD as local midnight, not UTC", () => {
    const d = parseDay("2025-08-14");
    expect([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours()]).toEqual([2025, 7, 14, 0]);
  });

  it("counts whole days between two dates", () => {
    expect(daysBetween("2026-08-16", "2026-10-24")).toBe(69);
    expect(daysBetween("2026-08-16", "2026-08-16")).toBe(0);
  });

  it("labels quarters and fiscal years", () => {
    expect(quarterLabel("2026-06-30")).toBe("Jun '26");
    expect(fiscalLabel(2026, "2026-03-31")).toBe("FY26");
    expect(fiscalLabel(2025, "2025-12-31")).toBe("FY25 (Dec)");
  });
});

describe("numbers", () => {
  it("groups digits the Indian way", () => {
    expect(num(1075675)).toBe("10,75,675");
    expect(num(-1234567.891, 1)).toBe("−12,34,567.9");
  });

  it("formats rupees", () => {
    expect(inr(1310)).toBe("₹1,310.00");
    expect(inr(-63.8)).toBe("−₹63.80");
  });

  it("formats fractions as percentages with a true minus", () => {
    expect(pct(-0.0464, 2, true)).toBe("−4.64%");
    expect(pct(0.9)).toBe("90.0%");
    expect(pct(0.0518, 1, true)).toBe("+5.2%");
  });

  it("writes ordinals including 11th-13th", () => {
    expect(ordinal(92)).toBe("92nd");
    expect(ordinal(21)).toBe("21st");
    expect(ordinal(11)).toBe("11th");
    expect(ordinal(13)).toBe("13th");
    expect(ordinal(3)).toBe("3rd");
  });

  it("labels sector z-scores", () => {
    expect(zLabel(15.1797)).toBe("+15.2 SD vs sector");
    expect(zLabel(-1.3844)).toBe("−1.4 SD vs sector");
  });

  it("shortens a model version to its hash", () => {
    expect(shortModel("phase2-composite-4b05e87d")).toBe("4b05e87d");
    expect(shortModel("plain")).toBe("plain");
  });
});

describe("rounding", () => {
  it("rounds decimal halves up, as a reader would", () => {
    expect(pct(0.0785)).toBe("7.9%");
    expect(zLabel(-1.25)).toBe("−1.3 SD vs sector");
    expect(num(0.5)).toBe("1");
  });
});

describe("todayIso", () => {
  it("is the date in India, not UTC", async () => {
    const { todayIso } = await import("@/lib/format");
    expect(todayIso(new Date("2026-10-23T20:00:00Z"))).toBe("2026-10-24");
  });
});
