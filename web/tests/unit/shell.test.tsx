import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import RootError from "@/app/error";
import { AppShell } from "@/components/shell/AppShell";
import { FreshnessCard } from "@/components/shell/FreshnessCard";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

describe("AppShell", () => {
  it("starts with a skip link to the content", () => {
    render(
      <AppShell freshness={{ scores: "2026-10-24" }} crumbs={[{ label: "Home" }]} today="2026-10-24">
        <p>Content</p>
      </AppShell>,
    );
    const skip = screen.getAllByRole("link")[0];
    expect(skip).toHaveTextContent("Skip to content");
    expect(skip).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });
});

describe("FreshnessCard", () => {
  it("says in words when data is stale, not only with an amber dot", () => {
    render(<FreshnessCard today="2026-10-24" scores="2026-08-16" />);
    expect(screen.getByText("16 Aug 2026").closest("[data-stale]")).toHaveTextContent("(over 7 days old)");
  });
});

describe("the outage page", () => {
  it("has a title and the site's navigation", () => {
    render(<RootError error={new Error("x")} retry={() => {}} />);
    expect(document.title).toBe("Data unavailable | StockAnalysis");
    expect(screen.getByRole("heading", { level: 1, name: "Data is temporarily unavailable" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /^Signals/ })[0]).toHaveAttribute("href", "/signals");
  });
});
