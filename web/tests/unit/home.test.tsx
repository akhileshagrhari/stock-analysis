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
  it("has the hero search, today's picture, short lists and the disclaimer", async () => {
    const data = await loadHome(get);
    render(
      <AppShell freshness={{ scores: data.asOf }} crumbs={[{ label: "Home" }]} today="2026-10-24">
        <HomeView data={data} />
      </AppShell>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Search NIFTY100 stocks" })).toBeInTheDocument();
    expect(screen.getAllByRole("combobox").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByTestId("today-counts")).toHaveTextContent("BUY 1 · HOLD 0 · SELL 3");
    expect(screen.getByText("4 of 100 companies scored on 16 Aug 2026; 96 not scored on that date.")).toBeInTheDocument();
    expect(within(screen.getByTestId("top-list")).getAllByRole("link")[0]).toHaveTextContent("ABB");
    expect(within(screen.getByTestId("flag-list")).getByRole("link", { name: /ADANIENSOL/ })).toHaveAttribute("href", "/stocks/ADANIENSOL");
    expect(screen.getByText(/Not investment advice\./)).toBeInTheDocument();
    expect(screen.getByText(/percentiles within the scored universe/i)).toBeInTheDocument();
  });
});
