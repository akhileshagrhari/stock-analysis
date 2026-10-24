import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InfoTip } from "@/components/ui/InfoTip";
import { describe, expect, it } from "vitest";
import { FreshnessCard } from "@/components/shell/FreshnessCard";
import { Disclaimer } from "@/components/shell/Disclaimer";

describe("FreshnessCard", () => {
  it("freshness dot is amber when older than 7 days", () => {
    render(<FreshnessCard today="2026-10-24" scores="2026-08-16" />);
    expect(screen.getByText("16 Aug 2026").closest("[data-stale]")).toHaveAttribute("data-stale", "true");
  });

  it("is not stale within 7 days, and shows each kind it is given", () => {
    render(<FreshnessCard today="2026-10-24" scores="2026-10-20" prices="2026-08-14" filings="2026-10-17" />);
    expect(screen.getByText("20 Oct 2026").closest("[data-stale]")).toHaveAttribute("data-stale", "false");
    expect(screen.getByText("14 Aug 2026").closest("[data-stale]")).toHaveAttribute("data-stale", "true");
    expect(screen.getByText("Filings")).toBeInTheDocument();
  });

  it("shows a dash when a date is unknown", () => {
    render(<FreshnessCard today="2026-10-24" scores={null} />);
    expect(screen.getByText("Scores").closest("[data-stale]")).toHaveTextContent("—");
  });
});

describe("Disclaimer", () => {
  it("carries the spec's text, marked pending legal review", () => {
    render(<Disclaimer />);
    expect(screen.getByText(/Not investment advice\./)).toBeInTheDocument();
    expect(document.body).toHaveTextContent(
      "Not investment advice. Scores and BUY / HOLD / SELL labels are outputs of a quantitative model that ranks NIFTY100 stocks against each other on a given date. They are not a recommendation to buy or sell any security and don't account for your circumstances. Data can be incomplete or late; check filings before acting. [Final wording pending legal review.]",
    );
  });
});

describe("InfoTip", () => {
  it("is a button that shows its text on tap, and hides it on a second tap or Escape", async () => {
    const user = userEvent.setup();
    render(<InfoTip text="What this figure means." />);
    const button = screen.getByRole("button", { name: "What this means" });
    expect(screen.queryByText("What this figure means.")).toBeNull();
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("What this figure means.")).toBeVisible();
    await user.click(button);
    expect(screen.queryByText("What this figure means.")).toBeNull();
    await user.click(button);
    await user.keyboard("{Escape}");
    expect(screen.queryByText("What this figure means.")).toBeNull();
  });
});
