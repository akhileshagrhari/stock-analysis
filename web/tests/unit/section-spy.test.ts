import { describe, expect, it } from "vitest";
import { pickSection } from "@/components/stock/SectionTabs";

const ids = ["overview", "breakdown", "history", "financials", "news", "peers"];

describe("pickSection", () => {
  it("takes the section whose top most recently passed the line, in page order not tab order", () => {
    const tops = { overview: -1549, breakdown: -701, news: -701, history: 64, financials: 976, peers: 1399 };
    expect(pickSection(ids, tops, 140, "overview")).toBe("history");
  });

  it("keeps the chosen one of two side-by-side sections", () => {
    const tops = { overview: -900, breakdown: 64, news: 64, history: 800, financials: 1700, peers: 2100 };
    expect(pickSection(ids, tops, 140, "news")).toBe("news");
    expect(pickSection(ids, tops, 140, "overview")).toBe("breakdown");
  });

  it("falls back to the first section above everything", () => {
    const tops = { overview: 300, breakdown: 1200, news: 1200, history: 2000, financials: 2800, peers: 3300 };
    expect(pickSection(ids, tops, 140, "history")).toBe("overview");
  });

  it("at the page foot, a deeper line reaches the last section", () => {
    const tops = { overview: -2332, breakdown: -1483, news: -1483, history: -718, financials: 194, peers: 617 };
    expect(pickSection(ids, tops, 900, "history")).toBe("peers");
  });
});
