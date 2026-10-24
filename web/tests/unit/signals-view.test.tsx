import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SignalsView } from "@/components/signals/SignalsView";
import { loadSignals } from "@/lib/signals/load";
import { fixtureHealth, fixtureModel, fixtureRedFlags, fixtureSectors, fixtureSignals } from "../fixtures";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace }) }));

const get = (async (path: string) => {
  if (path === "/model") return fixtureModel();
  if (path === "/health") return fixtureHealth();
  if (path === "/sectors") return fixtureSectors();
  if (path.startsWith("/signals/red-flags")) return fixtureRedFlags();
  const d = path.match(/as_of=([\d-]+)/)![1] as "2026-08-14" | "2026-08-15" | "2026-08-16";
  return ["2026-08-14", "2026-08-15", "2026-08-16"].includes(d) ? fixtureSignals(d) : [];
}) as <T>(path: string) => Promise<T | null>;

describe("SignalsView", () => {
  it("states the date and count, explains the partial run and links to the fuller list", async () => {
    render(<SignalsView data={await loadSignals({}, get)} />);
    expect(screen.getByText("4 stocks scored on 16 Aug 2026")).toBeInTheDocument();
    const notice = screen.getByText(/Only 4 of 100 companies were scored on 16 Aug 2026/);
    expect(within(notice.closest("div")!).getByRole("link", { name: /14 Aug 2026/ })).toHaveAttribute("href", "/signals?as_of=2026-08-14");
  });

  it("links each row to its stock page and marks low coverage", async () => {
    render(<SignalsView data={await loadSignals({ as_of: "2026-08-14" }, get)} />);
    const table = screen.getByRole("table");
    expect(within(table).getByRole("link", { name: /ADANIGREEN/ })).toHaveAttribute("href", "/stocks/ADANIGREEN");
    expect(table.querySelectorAll("[data-low-coverage]").length).toBeGreaterThan(0);
    expect(screen.getByTestId("signal-cards")).toBeInTheDocument();
  });

  it("shows the change since the previous date, with a dagger when either date was a partial run", async () => {
    render(<SignalsView data={await loadSignals({}, get)} />);
    const row = within(screen.getByRole("table")).getByRole("link", { name: /^ABB/ }).closest("tr")!;
    expect(row).toHaveTextContent("+43.3");
    expect(row).toHaveTextContent("†");
    expect(within(row).getByText("+43.3").className).not.toMatch(/up|down/);
    expect(screen.getByText(/one of the two dates was a partial run/)).toBeInTheDocument();
  });

  it("puts filters in the URL", async () => {
    const user = userEvent.setup();
    render(<SignalsView data={await loadSignals({}, get)} />);
    await user.click(screen.getByRole("button", { name: "BUY" }));
    expect(replace).toHaveBeenLastCalledWith("/signals?signal=BUY", { scroll: false });
    await user.selectOptions(screen.getByLabelText("Scored date"), "2026-08-14");
    expect(replace).toHaveBeenLastCalledWith("/signals?as_of=2026-08-14", { scroll: false });
  });

  it("sorts by a column", async () => {
    const user = userEvent.setup();
    render(<SignalsView data={await loadSignals({}, get)} />);
    await user.click(screen.getByRole("button", { name: /Sort by score/ }));
    const first = within(screen.getByRole("table")).getAllByRole("link")[0];
    expect(first).toHaveTextContent("RELIANCE");
  });
});
