import { describe, expect, it, vi } from "vitest";
import { counts, partialNotice, sortRows, withPrevious, type Row } from "@/lib/signals/list";
import { loadHome, loadSignals } from "@/lib/signals/load";
import { fixtureHealth, fixtureModel, fixtureRedFlags, fixtureSectors, fixtureSignals } from "../fixtures";

/** Answers like the API from the captured fixtures, recording each path. */
function apiStub() {
  return vi.fn(async (path: string) => {
    if (path === "/model") return fixtureModel();
    if (path === "/health") return fixtureHealth();
    if (path === "/sectors") return fixtureSectors();
    if (path.startsWith("/signals/red-flags")) return fixtureRedFlags();
    const m = path.match(/^\/signals\?as_of=(\d{4}-\d{2}-\d{2})&limit=1000$/);
    if (m) {
      const d = m[1];
      return d === "2026-08-14" || d === "2026-08-15" || d === "2026-08-16" ? fixtureSignals(d) : [];
    }
    throw new Error(`unexpected ${path}`);
  }) as unknown as (<T>(path: string) => Promise<T | null>) & { mock: { calls: string[][] } };
}

describe("signals list logic", () => {
  const rows = withPrevious(fixtureSignals("2026-08-16"), fixtureSignals("2026-08-15"));

  it("joins the previous date's score by ISIN", () => {
    expect(rows.find((r) => r.nse_symbol === "ABB")!.delta!.toFixed(1)).toBe("43.3");
    expect(rows.find((r) => r.nse_symbol === "RELIANCE")!.delta).toBeNull();
  });

  it("flags a change of model between the two dates", () => {
    const cross = withPrevious(fixtureSignals("2026-08-16"), fixtureSignals("2026-08-14"));
    const rel = cross.find((r) => r.nse_symbol === "RELIANCE")!;
    expect(rel.modelChanged).toBe(true);
    expect(rel.delta!.toFixed(1)).toBe("-4.7");
  });

  it("sorts null scores last in both directions", () => {
    const withNull = [...rows, { ...rows[0], isin: "X", nse_symbol: "ZZZ", composite_score: null } as Row];
    expect(sortRows(withNull, "score", "asc").at(-1)!.nse_symbol).toBe("ZZZ");
    expect(sortRows(withNull, "score", "desc").at(-1)!.nse_symbol).toBe("ZZZ");
    expect(sortRows(withNull, "score", "desc")[0].nse_symbol).toBe("ABB");
    expect(sortRows(withNull, "symbol", "asc")[0].nse_symbol).toBe("ABB");
  });

  it("explains a partial date", () => {
    expect(partialNotice("2026-08-16", 4, 100, { date: "2026-08-14", count: 77 })).toBe(
      "Only 4 of 100 companies were scored on 16 Aug 2026. The most recent fuller list is 14 Aug 2026 (77 companies).",
    );
    expect(partialNotice("2026-08-14", 77, 100, null)).toBeNull();
    expect(partialNotice("2026-08-16", 4, 100, null)).toBe("Only 4 of 100 companies were scored on 16 Aug 2026.");
  });

  it("counts signals", () => {
    expect(counts(fixtureSignals("2026-08-14"))).toEqual({ BUY: 16, HOLD: 19, SELL: 42 });
    expect(counts(fixtureSignals("2026-08-16"))).toEqual({ BUY: 1, HOLD: 0, SELL: 3 });
  });
});

describe("loadSignals", () => {
  it("defaults to the latest date, joins the previous one and finds the fuller list", async () => {
    const get = apiStub();
    const d = await loadSignals({}, get);
    expect(d.asOf).toBe("2026-08-16");
    expect(d.previousAsOf).toBe("2026-08-15");
    expect(d.total).toBe(4);
    expect(d.rows).toHaveLength(4);
    expect(d.fuller).toEqual({ date: "2026-08-14", count: 77 });
    expect(d.notice).toContain("Only 4 of 100 companies");
    expect(d.rows.find((r) => r.nse_symbol === "ABB")!.delta!.toFixed(1)).toBe("43.3");
  });

  it("filters by signal and sector without changing the date's total", async () => {
    const d = await loadSignals({ as_of: "2026-08-14", signal: "BUY", sector: "Capital Goods" }, apiStub());
    expect(d.total).toBe(77);
    expect(d.notice).toBeNull();
    expect(d.rows.every((r) => r.signal === "BUY" && r.sector === "Capital Goods")).toBe(true);
  });

  it("ignores an unknown date or signal rather than erroring", async () => {
    const d = await loadSignals({ as_of: "1999-01-01", signal: "MAYBE" }, apiStub());
    expect(d.asOf).toBe("2026-08-16");
    expect(d.filters.signal).toBeNull();
  });
});

describe("loadHome", () => {
  it("summarises the latest date", async () => {
    const h = await loadHome(apiStub());
    expect(h.asOf).toBe("2026-08-16");
    expect(h.counts).toEqual({ BUY: 1, HOLD: 0, SELL: 3 });
    expect(h.notScored).toBe(96);
    expect(h.top[0].nse_symbol).toBe("ABB");
    expect(h.flagged.map((r) => r.nse_symbol)).toContain("ADANIENSOL");
    expect(h.fuller).toEqual({ date: "2026-08-14", count: 77 });
    const shown = [...h.top, ...h.bottom].map((r) => r.nse_symbol);
    expect(new Set(shown).size).toBe(shown.length);
  });
});
