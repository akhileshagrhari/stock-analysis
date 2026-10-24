import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HomeView } from "@/components/home/HomeView";
import { AppShell } from "@/components/shell/AppShell";
import { loadHome } from "@/lib/signals/load";
import { fixtureHealth, fixtureModel, fixtureRedFlags, fixtureSectors, fixtureSignals } from "../fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const get = (async (path: string) => {
  if (path === "/model") return fixtureModel();
  if (path === "/health") return fixtureHealth();
  if (path === "/sectors") return fixtureSectors();
  if (path.startsWith("/signals/red-flags")) return fixtureRedFlags();
  const d = path.match(/as_of=([\d-]+)/)![1] as "2026-08-14" | "2026-08-15" | "2026-08-16";
  return ["2026-08-14", "2026-08-15", "2026-08-16"].includes(d) ? fixtureSignals(d) : [];
}) as <T>(path: string) => Promise<T | null>;

describe("HomeView", () => {
  it("has the hero search, latest scores, short lists and the disclaimer", async () => {
    const data = await loadHome(get);
    render(
      <AppShell freshness={{ scores: data.asOf }} crumbs={[{ label: "Home" }]} today="2026-10-24">
        <HomeView data={data} />
      </AppShell>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Search NIFTY100 stocks" })).toBeInTheDocument();
    expect(screen.getAllByRole("combobox").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("heading", { level: 2, name: /^Latest scores \(16 Aug 2026\)/ })).toBeInTheDocument();
    expect(screen.queryByText(/Today/)).toBeNull();
    expect(screen.getByTestId("today-counts")).toHaveTextContent("BUY 1 · HOLD 0 · SELL 3");
    expect(within(screen.getByTestId("lists-note")).getByText("Model ranks on 14 Aug 2026, not investment advice.")).toBeVisible();
    expect(screen.getByText("4 of 100 companies scored on 16 Aug 2026; 96 not scored on that date.")).toBeInTheDocument();
    // The 16 Aug run ranked 4 companies, so the lists use 14 Aug and say so.
    expect(screen.getByText("The lists below use 14 Aug 2026.")).toBeInTheDocument();
    expect(within(screen.getByTestId("top-list")).getAllByRole("link")).toHaveLength(5);
    expect(screen.getByTestId("top-list").closest("section, div")!.parentElement).toHaveTextContent("14 Aug 2026");
    expect(within(screen.getByTestId("flag-list")).getByRole("link", { name: /CHOLAFIN/ })).toHaveAttribute("href", "/stocks/CHOLAFIN");
    expect(screen.getByTestId("flag-list").closest("div")!.parentElement).toHaveTextContent(/Red flags\s*14 Aug 2026/);
    expect(screen.getByText(/Not investment advice\./)).toBeInTheDocument();
    expect(screen.getByText(/percentiles within the scored universe/i)).toBeInTheDocument();
  });
});
