import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DivergingBar } from "@/components/charts/DivergingBar";
import { HalfGauge } from "@/components/charts/HalfGauge";
import { PriceChart } from "@/components/charts/PriceChart";
import { ScaleBar } from "@/components/charts/ScaleBar";
import { ScoreHistoryChart } from "@/components/charts/ScoreHistoryChart";
import { SegmentMeter } from "@/components/charts/SegmentMeter";
import { Sparkline } from "@/components/charts/Sparkline";
import { slicePeriod, toCloses } from "@/lib/prices";
import { snapshots } from "@/lib/stock/history";
import { fixtureStock } from "../fixtures";

const thresholds = { sell: 45, buy: 75 };
const relHistory = () => snapshots(fixtureStock("RELIANCE").history);

describe("SegmentMeter", () => {
  it("places the marker at the score", () => {
    render(<SegmentMeter score={28.4} thresholds={thresholds} />);
    expect(screen.getByTestId("meter-marker")).toHaveStyle({ left: "28.4%" });
    expect(screen.getByRole("img")).toHaveAccessibleName(/28 out of 100/);
  });

  it("has no marker for a null score (Review Focus 5)", () => {
    const { container } = render(<SegmentMeter score={null} thresholds={thresholds} />);
    expect(screen.queryByTestId("meter-marker")).toBeNull();
    expect(container.innerHTML).not.toMatch(/NaN/);
  });
});

describe("DivergingBar", () => {
  it("renders null as hatched, not NaN", () => {
    const { container } = render(<DivergingBar value={null} max={0.45} />);
    expect(container.querySelector("[data-hatched]")).not.toBeNull();
    expect(container.innerHTML).not.toMatch(/NaN/);
  });

  it("grows a negative value left of centre", () => {
    render(<DivergingBar value={-0.178} max={0.45} />);
    expect(screen.getByTestId("bar")).toHaveStyle({ right: "50%" });
  });

  it("grows a positive value right of centre, capped at half", () => {
    render(<DivergingBar value={0.9} max={0.45} />);
    expect(screen.getByTestId("bar")).toHaveStyle({ left: "50%", width: "50%" });
  });
});

describe("Sparkline", () => {
  it("draws one dashed marker per model change", () => {
    const { container } = render(<Sparkline points={relHistory()} />);
    expect(container.querySelectorAll("[data-model-change]")).toHaveLength(1);
    expect(container.querySelectorAll("circle")).toHaveLength(3);
  });

  it("draws a single snapshot as one dot without NaN", () => {
    const { container } = render(<Sparkline points={snapshots(fixtureStock("ADANIENSOL").history)} />);
    expect(container.querySelectorAll("circle")).toHaveLength(1);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
  });
});

describe("ScoreHistoryChart", () => {
  it("shades the bands, marks the model change and reports coverage per point", () => {
    const { container } = render(<ScoreHistoryChart points={relHistory()} thresholds={thresholds} />);
    expect(container.querySelectorAll("[data-band]")).toHaveLength(3);
    expect(container.querySelectorAll("[data-model-change]")).toHaveLength(1);
    expect(container.querySelector("title")?.textContent).toMatch(/31 Jul 2026.*32\.5.*23% of factors/);
    expect(container.innerHTML).not.toMatch(/NaN/);
  });

  it("skips null scores rather than plotting them at 0", () => {
    const pts = relHistory();
    pts[1] = { ...pts[1], score: null, signal: null };
    const { container } = render(<ScoreHistoryChart points={pts} thresholds={thresholds} />);
    expect(container.querySelectorAll("circle[data-point]")).toHaveLength(2);
    expect(container.innerHTML).not.toMatch(/NaN/);
  });
});

describe("HalfGauge", () => {
  it("has a needle for a value and none for null", () => {
    const { rerender } = render(<HalfGauge value={0.5} />);
    expect(screen.getByTestId("needle")).toBeInTheDocument();
    rerender(<HalfGauge value={null} />);
    expect(screen.queryByTestId("needle")).toBeNull();
  });
});

describe("ScaleBar", () => {
  it("marks the score and the gap label", () => {
    render(<ScaleBar score={28.4} thresholds={thresholds} label="17 pts to HOLD" />);
    expect(screen.getByTestId("scale-now")).toHaveStyle({ left: "28.4%" });
    expect(screen.getByText("17 pts to HOLD")).toBeInTheDocument();
  });

  it("has no dot for a null score", () => {
    const { container } = render(<ScaleBar score={null} thresholds={thresholds} label="" />);
    expect(screen.queryByTestId("scale-now")).toBeNull();
    expect(container.innerHTML).not.toMatch(/NaN/);
  });
});

describe("PriceChart", () => {
  const closes = slicePeriod(toCloses(fixtureStock("RELIANCE").prices), "1Y");

  it("has an accessible summary", () => {
    render(<PriceChart closes={closes} />);
    expect(screen.getByRole("img", { name: /price/i })).toHaveAccessibleName(/14 Aug 2025.*14 Aug 2026.*₹1,373\.80.*₹1,310\.00/);
  });

  it("draws one line and a right-hand axis", () => {
    const { container } = render(<PriceChart closes={closes} />);
    expect(container.querySelectorAll("path[data-line]")).toHaveLength(1);
    expect(container.querySelectorAll("[data-ytick]").length).toBeGreaterThanOrEqual(3);
  });

  it("survives a single bar and a flat series", () => {
    const one = render(<PriceChart closes={[["2026-08-14", 1310]]} />);
    expect(one.container.innerHTML).not.toMatch(/NaN|Infinity/);
    one.unmount();
    const flat = render(<PriceChart closes={[["2026-08-13", 100], ["2026-08-14", 100]]} />);
    expect(flat.container.innerHTML).not.toMatch(/NaN|Infinity/);
  });
});

describe("date labels", () => {
  it("drops a middle label that would collide with its neighbour", async () => {
    const { labelIndexes } = await import("@/components/charts/ScoreHistoryChart");
    // x positions as on a 390px phone: 31 Jul, 14 Aug, 16 Aug
    expect(labelIndexes([30, 300, 330], 48)).toEqual([0, 2]);
    expect(labelIndexes([30, 200, 330], 48)).toEqual([0, 1, 2]);
    expect(labelIndexes([100], 48)).toEqual([0]);
  });
});
