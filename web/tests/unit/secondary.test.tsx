import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DisclaimerView } from "@/components/learn/DisclaimerView";
import { LearnView } from "@/components/learn/LearnView";
import { RedFlagsView } from "@/components/learn/RedFlagsView";
import { fixtureModel, fixtureRedFlags } from "../fixtures";

describe("LearnView", () => {
  it("shows the families, weights and thresholds from the model", () => {
    render(<LearnView model={fixtureModel()} />);
    expect(screen.getByText("Scores are percentiles within the scored universe on each date, not valuations.")).toBeInTheDocument();
    const weights = screen.getByRole("table", { name: "Family weights" });
    expect(within(weights).getByText("Quality")).toBeInTheDocument();
    expect(within(weights).getByText("30%")).toBeInTheDocument();
    expect(screen.getByText(/BUY from 75/)).toBeInTheDocument();
    expect(document.getElementById("model")).not.toBeNull();
  });
});

describe("RedFlagsView", () => {
  it("lists flagged companies and the six-flag catalogue", () => {
    render(<RedFlagsView flagged={fixtureRedFlags()} asOf="2026-08-16" />);
    expect(screen.getByRole("link", { name: /ADANIENSOL/ })).toHaveAttribute("href", "/stocks/ADANIENSOL");
    const catalogue = screen.getByRole("table", { name: "Red-flag checks" });
    expect(within(catalogue).getAllByRole("row")).toHaveLength(7);
    expect(within(catalogue).getAllByText("No data source yet")).toHaveLength(2);
  });

  it("says when nothing is flagged", () => {
    render(<RedFlagsView flagged={[]} asOf="2026-08-16" />);
    expect(screen.getByText("No company has a triggered red flag on 16 Aug 2026.")).toBeInTheDocument();
  });
});

describe("DisclaimerView", () => {
  it("carries the short text and marks the page pending legal review", () => {
    render(<DisclaimerView />);
    expect(screen.getByRole("heading", { level: 1, name: "Disclaimer" })).toBeInTheDocument();
    expect(screen.getAllByText(/pending legal review/i).length).toBeGreaterThan(0);
  });
});
