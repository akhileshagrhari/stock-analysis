import { describe, expect, it, vi } from "vitest";
import { ApiUnavailableError } from "@/lib/api/client";
import { loadStockPage } from "@/lib/stock/load";
import { fixtureHealth, fixtureSignals, fixtureStock } from "../fixtures";

/** A getter that answers like the API for RELIANCE, whatever symbol is asked. */
function fixtureGetter(symbol: "RELIANCE" | "HDFCBANK" = "RELIANCE") {
  const d = fixtureStock(symbol);
  return vi.fn(async (path: string) => {
    if (path.startsWith("/instruments/by-symbol/")) return d.instrument;
    if (path.endsWith("/explanation")) return d.explanation;
    if (path.includes("/history")) return d.history;
    if (path.includes("/prices")) return d.prices;
    if (path.includes("/financials")) return d.financials;
    if (path.includes("/news")) return d.news;
    if (path === "/model") return d.model;
    if (path === "/health") return fixtureHealth();
    const run = path.match(/^\/signals\?as_of=(\d{4}-\d{2}-\d{2})&limit=1000$/);
    if (run) return ["2026-08-14", "2026-08-15", "2026-08-16"].includes(run[1]) ? fixtureSignals(run[1] as "2026-08-14") : null;
    if (path.startsWith("/signals")) return d.peers;
    throw new Error(`unexpected ${path}`);
  }) as unknown as (<T>(path: string) => Promise<T | null>) & { mock: { calls: string[][] } };
}

describe("loadStockPage", () => {
  it("counts the companies in each of the company's scored runs", async () => {
    const data = await loadStockPage("RELIANCE", fixtureGetter());
    expect(data!.runs.instruments).toBe(100);
    expect(data!.runs.counts).toMatchObject({ "2026-08-14": 77, "2026-08-16": 4 });
  });

  it("upper-cases and encodes the symbol (Review Focus 1)", async () => {
    const get = fixtureGetter();
    await loadStockPage("m&m", get);
    expect(get).toHaveBeenCalledWith("/instruments/by-symbol/M%26M");
  });

  it("keeps hyphenated symbols intact", async () => {
    const get = fixtureGetter();
    await loadStockPage("bajaj-auto", get);
    expect(get).toHaveBeenCalledWith("/instruments/by-symbol/BAJAJ-AUTO");
  });

  it("is null for an unknown symbol", async () => {
    const get = vi.fn(async () => null);
    expect(await loadStockPage("NOPE", get)).toBeNull();
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("throws ApiUnavailableError when the API is down (Review Focus 2)", async () => {
    const get = vi.fn(async () => {
      throw new ApiUnavailableError(503, "/instruments/by-symbol/RELIANCE");
    });
    await expect(loadStockPage("RELIANCE", get)).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("fetches every section and the same-date peers", async () => {
    const get = fixtureGetter();
    const data = (await loadStockPage("RELIANCE", get))!;
    const paths = get.mock.calls.map((c) => c[0]);
    expect(paths).toEqual(
      expect.arrayContaining([
        "/instruments/INE002A01018/explanation",
        "/instruments/INE002A01018/history?limit=500",
        "/instruments/INE002A01018/prices?from=2000-01-01",
        "/instruments/INE002A01018/financials?quarters=12",
        "/instruments/INE002A01018/news?limit=20",
        "/model",
        "/signals?sector=Oil%20Gas%20%26%20Consumable%20Fuels&as_of=2026-08-16&limit=1000",
      ]),
    );
    expect(data.peersAsOf).toBe("2026-08-16");
    expect(data.explanation?.nse_symbol).toBe("RELIANCE");
  });

  it("gives an unscored company's peers from the latest full run, not a partial one", async () => {
    const get = fixtureGetter("HDFCBANK");
    const data = (await loadStockPage("HDFCBANK", get))!;
    expect(data.explanation).toBeNull();
    // 16 Aug scored 4 companies and 15 Aug 1; 14 Aug scored 77.
    expect(data.peersAsOf).toBe("2026-08-14");
    expect(get).toHaveBeenCalledWith("/signals?sector=Financial%20Services&as_of=2026-08-14&limit=1000");
  });
});
