import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { StockPageView } from "@/components/stock/StockPageView";
import { fixtureStock, type FixtureSymbol } from "../fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const renderView = (symbol: FixtureSymbol) => render(<StockPageView data={fixtureStock(symbol)} />);
const section = (id: string) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`no #${id}`);
  return el;
};
const rowNamed = (label: string) => within(section("financials")).getByText(label).closest("tr") as HTMLElement;

describe("RELIANCE lower sections", () => {
  it("breakdown: percentiles, an unmeasured family and the footnote", () => {
    renderView("RELIANCE");
    const b = section("breakdown");
    expect(within(b).getByText("92nd")).toBeInTheDocument();
    expect(within(b).getByText("n/m")).toBeInTheDocument();
    expect(within(b).getByText(/Sentiment wasn't measured, so the others were re-weighted/)).toBeInTheDocument();
    expect(within(b).getByText(/Percentile within the scored universe on 16 Aug 2026/)).toBeInTheDocument();
  });

  it("drivers: z-score, raw value and what positive means, both ways", async () => {
    const user = userEvent.setup();
    renderView("RELIANCE");
    expect(screen.getByText("+15.2 SD vs sector")).toBeInTheDocument();
    expect(screen.getByText("1.04×")).toBeInTheDocument();
    expect(screen.getByText("Positive = cheap on book value")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pulling down" }));
    expect(screen.getByText("−1.4 SD vs sector")).toBeInTheDocument();
    expect(screen.getByText("Positive = stronger price trend")).toBeInTheDocument();
    expect(screen.queryByText(/^Weak:/)).toBeNull();
  });

  it("news: scored first, unscored items labelled", async () => {
    const user = userEvent.setup();
    renderView("RELIANCE");
    const news = section("news");
    expect(within(news).queryByText("Not scored yet")).toBeNull();
    await user.click(within(news).getByRole("button", { name: "All 19" }));
    expect(within(news).getAllByText("Not scored yet").length).toBeGreaterThan(0);
    const link = within(news).getAllByRole("link")[0];
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("history: compare across a model change", async () => {
    const user = userEvent.setup();
    renderView("RELIANCE");
    await user.selectOptions(screen.getByLabelText("Compare with"), "2026-07-31");
    const h = section("history");
    expect(within(h).getByText(/not strictly comparable/)).toBeInTheDocument();
    expect(within(h).getByText(/Family changes: coming soon/)).toBeInTheDocument();
    expect(within(h).getAllByRole("row")).toHaveLength(4); // header + 3 snapshots
  });

  it("financials: quarterly then annual, with gaps marked", async () => {
    const user = userEvent.setup();
    renderView("RELIANCE");
    expect(within(section("financials")).getByText("Jun '26")).toBeInTheDocument();
    await user.click(within(section("financials")).getByRole("button", { name: "Annual" }));
    expect(within(section("financials")).getByText("10,75,675")).toBeInTheDocument();
    expect(within(section("financials")).getByText("FY26")).toBeInTheDocument();
    expect(within(rowNamed("EBITDA")).getAllByTitle("Not in the filing").length).toBeGreaterThan(0);
    expect(within(section("financials")).getByText(/₹ crore/)).toBeInTheDocument();
  });

  it("peers: says so when no other company in the sector was scored that day", () => {
    renderView("RELIANCE");
    expect(screen.getByText("No other Oil Gas & Consumable Fuels company was scored on 16 Aug 2026")).toBeInTheDocument();
  });
});

describe("other companies", () => {
  it("ABB: Dec fiscal year", async () => {
    const user = userEvent.setup();
    renderView("ABB");
    await user.click(within(section("financials")).getByRole("button", { name: "Annual" }));
    expect(screen.getByText("FY25 (Dec)")).toBeInTheDocument();
  });

  it("HDFCBANK: no annual figures, no breakdown or history", async () => {
    const user = userEvent.setup();
    renderView("HDFCBANK");
    await user.click(within(section("financials")).getByRole("button", { name: "Annual" }));
    expect(screen.getByText("No annual figures in this company's filings")).toBeInTheDocument();
    expect(document.getElementById("breakdown")).toBeNull();
    expect(document.getElementById("history")).toBeNull();
    expect(document.getElementById("news")).not.toBeNull();
  });

  it("CHOLAFIN: stale breakdown and the company highlighted among peers", () => {
    renderView("CHOLAFIN");
    expect(screen.getByText(/re-derived with current weights/)).toBeInTheDocument();
    expect(within(section("breakdown")).getByText(/Value, quality and sentiment weren't measured/)).toBeInTheDocument();
    const peers = section("peers");
    const me = within(peers).getByText("CHOLAFIN").closest("tr")!;
    expect(me).toHaveAttribute("aria-current", "true");
    expect(within(peers).getAllByRole("row").length).toBe(8); // header + 7 companies
  });

  it("ADANIENSOL: one snapshot, nothing to compare", () => {
    renderView("ADANIENSOL");
    expect(within(section("history")).getByText(/Only one snapshot so far/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Compare with")).toBeNull();
  });
});
