import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { StockPageView } from "@/components/stock/StockPageView";
import type { StockPageData } from "@/lib/stock/load";
import { fixtureStock, type FixtureSymbol } from "../fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

function renderView(symbol: FixtureSymbol, patch: (d: StockPageData) => StockPageData = (d) => d) {
  return render(<StockPageView data={patch(fixtureStock(symbol))} />);
}

describe("stock page top: RELIANCE (scored, ordinary)", () => {
  it("shows the score, signal, date and model", () => {
    renderView("RELIANCE");
    expect(screen.getByTestId("score-value")).toHaveTextContent(/^28\.4 \/ 100$/);
    expect(screen.getAllByText("SELL").length).toBeGreaterThan(0);
    expect(screen.getByText("Scored 16 Aug 2026 · model 4b05e87d")).toBeInTheDocument();
  });

  it("explains why", () => {
    renderView("RELIANCE");
    const why = screen.getByTestId("why-card");
    expect(within(why).getByText("16.6 pts to HOLD")).toBeInTheDocument();
    expect(within(why).getByText("Data behind 90% of the model's weight · 19 of 20 factors")).toBeInTheDocument();
    expect(within(why).getByText("Value pulls the score up; quality and momentum pull it down")).toBeInTheDocument();
    expect(within(why).getByText("Value 92nd")).toBeInTheDocument();
    expect(within(why).getByText("Momentum 21st")).toBeInTheDocument();
  });

  it("dates the price and the checks", () => {
    renderView("RELIANCE");
    expect(screen.getByText("NSE close · 14 Aug 2026")).toBeInTheDocument();
    expect(screen.getByText("Unchecked ≠ clean")).toBeInTheDocument();
    expect(screen.getByText("1 positive · 1 negative · 1 neutral")).toBeInTheDocument();
    expect(screen.getByText("Too few articles to score sentiment")).toBeInTheDocument();
  });

  it("shows the 1Y price change by default", () => {
    renderView("RELIANCE");
    expect(screen.getByText("₹1,310.00")).toBeInTheDocument();
    expect(screen.getByTestId("price-change")).toHaveTextContent("4.64%");
  });

  it("shows the score trend since first scored", () => {
    renderView("RELIANCE");
    const trend = screen.getByTestId("trend-card");
    expect(within(trend).getByText("−4.1")).toBeInTheDocument();
    expect(within(trend).getByText("points since first scored, 31 Jul 2026")).toBeInTheDocument();
  });

  it("marks four flags unchecked and none triggered", () => {
    renderView("RELIANCE");
    const flags = screen.getByTestId("redflags-card");
    expect(within(flags).getByText("4 unchecked")).toBeInTheDocument();
    expect(within(flags).getByTestId("flags-triggered")).toHaveTextContent("0");
  });
});

describe("stock page top: HDFCBANK (unscored)", () => {
  it("never shows a 0, and hides score-derived cards", () => {
    renderView("HDFCBANK");
    expect(screen.getByText("Not scored")).toBeInTheDocument();
    expect(screen.queryByText(/\/ 100/)).toBeNull();
    expect(screen.getByText(/banks and NBFCs/)).toBeInTheDocument();
    expect(screen.queryByTestId("why-card")).toBeNull();
    expect(screen.queryByTestId("trend-card")).toBeNull();
    expect(screen.queryByTestId("redflags-card")).toBeNull();
    expect(screen.getByText(/NSE close · /)).toBeInTheDocument();
  });
});

describe("stock page top: CHOLAFIN (red-flag override, low coverage)", () => {
  it("explains the forced SELL and the low coverage", () => {
    renderView("CHOLAFIN");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "SELL forced by red flag: Promoter holding falling 3 consecutive quarters. Without it, the score would read BUY.",
    );
    expect(screen.getByText("Data behind 23% of the model's weight (5 of 20 factors), treat with caution")).toBeInTheDocument();
    expect(screen.getByTestId("score-value")).toHaveTextContent("87");
  });
});

describe("stock page top: other states", () => {
  it("ABB: no scored news means no needle", () => {
    renderView("ABB");
    expect(screen.getByText("No scored news in 30 days")).toBeInTheDocument();
    expect(screen.queryByTestId("needle")).toBeNull();
  });

  it("ADANIENSOL: a single snapshot", () => {
    renderView("ADANIENSOL");
    expect(screen.getByText("First scored 16 Aug 2026")).toBeInTheDocument();
  });

  it("no prices stored (Review Focus 3)", () => {
    renderView("RELIANCE", (d) => ({ ...d, prices: [] }));
    expect(screen.getByText("No price data stored")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it("below the coverage floor (Review Focus 5)", () => {
    renderView("RELIANCE", (d) => ({ ...d, explanation: { ...d.explanation!, composite_score: null, signal: null } }));
    expect(screen.getByText("Not scored on 16 Aug 2026: data behind only 90% of the model's weight.")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toMatch(/NaN/);
  });

  it("a HOLD just under the BUY line never reads 75", () => {
    renderView("RELIANCE", (d) => ({ ...d, explanation: { ...d.explanation!, composite_score: 74.96, signal: "HOLD", red_flags: [] } }));
    expect(screen.getByTestId("score-value")).toHaveTextContent(/^74\.9 \/ 100$/);
    expect(document.body.textContent).not.toMatch(/Score\s*75/);
  });

  it("shows the narrative when there is one", () => {
    renderView("RELIANCE", (d) => ({ ...d, explanation: { ...d.explanation!, narrative: "Test narrative." } }));
    expect(within(screen.getByTestId("why-card")).getByText("Test narrative.")).toBeInTheDocument();
  });

  it("has the header and in-page tabs", () => {
    renderView("RELIANCE");
    expect(screen.getByRole("heading", { level: 1, name: "RELIANCE" })).toBeInTheDocument();
    expect(screen.getByText("Reliance Industries Ltd.")).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Sections" })).getByRole("link", { name: "Score history" })).toHaveAttribute("href", "#history");
  });
});
