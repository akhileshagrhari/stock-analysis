import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  refresh: vi.fn(),
  redirect: vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  }),
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND");
  }),
  userGet: vi.fn(),
  accountsEnabled: vi.fn(() => true),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, refresh: mocks.refresh, replace: vi.fn() }),
  usePathname: () => "/portfolio",
  redirect: mocks.redirect,
  notFound: mocks.notFound,
}));
vi.mock("next/server", () => ({ connection: async () => {} }));
vi.mock("next/headers", () => ({ headers: async () => new Headers() }));
// user.ts imports the Better Auth instance; keep that out of the tests.
vi.mock("@/lib/auth/server", () => ({ apiToken: vi.fn(), currentUser: vi.fn() }));
vi.mock("@/lib/auth/config", async (orig) => ({ ...(await orig<typeof import("@/lib/auth/config")>()), accountsEnabled: mocks.accountsEnabled }));
vi.mock("@/lib/auth/client", () => ({ authClient: { useSession: vi.fn(), signOut: vi.fn(), deleteUser: vi.fn() } }));
vi.mock("@/lib/api/user", async (orig) => ({ ...(await orig<typeof import("@/lib/api/user")>()), userGet: mocks.userGet }));

import PortfolioPage from "@/app/portfolio/page";
import { AccountMenu } from "@/components/account/AccountMenu";
import { ImportForm } from "@/components/portfolio/ImportForm";
import { PortfolioView } from "@/components/portfolio/PortfolioView";
import { ApiUnavailableError } from "@/lib/api/client";
import type { Holding, Portfolio } from "@/lib/api/types";
import { SignedOutError } from "@/lib/api/user";
import { authClient } from "@/lib/auth/client";
import { fixturePortfolio } from "../fixtures";

const OUTSIDE_UNIVERSE = "Outside the NIFTY 100 companies the model covers. ETFs, gold bonds and REITs are never rated.";
const ADVICE = /sell this|you should|we recommend/i;

const fetchMock = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  mocks.accountsEnabled.mockReturnValue(true);
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(authClient.useSession).mockReturnValue({ data: null, isPending: false } as never);
});

/** The fixture with some holdings changed, everything else as captured. */
function edited(patch: Record<string, Partial<Holding>>, top: Partial<Portfolio> = {}): Portfolio {
  const p = fixturePortfolio();
  return { ...p, ...top, holdings: p.holdings.map((h) => (patch[h.symbol] ? { ...h, ...patch[h.symbol] } : h)) };
}

function row(symbol: string): HTMLElement {
  const cell = screen.getByText(symbol, { selector: "a, b" });
  return cell.closest("tr") as HTMLElement;
}

function section(group: string): HTMLElement {
  return document.querySelector(`tbody[data-group="${group}"]`) as HTMLElement;
}

describe("PortfolioView", () => {
  it("captures the dev data's portfolio for the fixture CSV", () => {
    const p = fixturePortfolio();
    expect(p.holdings).toHaveLength(23);
    expect(p.last_import).toMatchObject({ filename: "kite_holdings.csv", mode: "replace", rows: 23 });
  });

  it("renders the groups in BUY, HOLD, SELL, Not rated order, each with its own holdings", () => {
    const p = fixturePortfolio();
    render(<PortfolioView data={p} saved={false} />);
    const groups = [...document.querySelectorAll("tbody[data-group]")].map((el) => el.getAttribute("data-group"));
    expect(groups).toEqual(["BUY", "HOLD", "SELL", "NOT_RATED"]);
    const heads = groups.map((g) => within(section(g!)).getByRole("rowheader"));
    expect(heads.map((h) => h.textContent)).toEqual([
      "BUY2 holdings · 11.8%",
      "HOLD2 holdings · 19.8%",
      "SELL9 holdings · 34.5%",
      "Not rated10 holdings · 33.9%",
    ]);
    for (const h of p.holdings) {
      expect(section(h.group)).toContainElement(row(h.symbol));
    }
    // Largest value first within a group, as the API sent them.
    const buy = within(section("BUY")).getAllByRole("link").map((a) => a.textContent);
    expect(buy).toEqual(["TMCV", "TMPV"]);
  });

  it("leaves out a group with no holdings", () => {
    const p = fixturePortfolio();
    const data = {
      ...p,
      holdings: p.holdings.filter((h) => h.group !== "HOLD"),
      groups: p.groups.map((g) => (g.group === "HOLD" ? { ...g, holdings: 0, value: 0, share: 0 } : g)),
    };
    render(<PortfolioView data={data} saved={false} />);
    expect(section("HOLD")).toBeNull();
    expect(section("BUY")).not.toBeNull();
  });

  it("gives a legend whose percentages are the groups' shares, and a bar to match", () => {
    const p = fixturePortfolio();
    render(<PortfolioView data={p} saved={false} />);
    const legend = screen.getByRole("list", { name: "Share of value by rating" });
    expect(within(legend).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "BUY 11.8%",
      "HOLD 19.8%",
      "SELL 34.5%",
      "Not rated 33.9%",
    ]);
    const widths = [...document.querySelectorAll("[data-testid=share-bar] > span")].map((el) => (el as HTMLElement).style.width);
    expect(widths).toEqual(p.groups.map((g) => `${g.share * 100}%`));
  });

  it("shows value, invested and profit or loss, with the model-rank note", () => {
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    expect(screen.getByText("Current value").parentElement).toHaveTextContent("Current value₹14,21,682.27");
    expect(screen.getByText("Invested").parentElement).toHaveTextContent("Invested₹13,39,544.65");
    expect(screen.getByText("Profit / loss").parentElement).toHaveTextContent("Profit / loss+₹82,137.62 · +6.1%");
    expect(screen.getByText(/^Model ranks on 24 Oct 2026, not investment advice\.$/)).toBeInTheDocument();
    expect(screen.queryByText(/have no price/)).toBeNull();
  });

  it("counts holdings with no price out, and says so", () => {
    const data = edited({ NIFTYBEES: { price: null, price_date: null, price_source: null, value: null, pnl: null, pnl_pct: null, weight: null } }, { unpriced: 2 });
    render(<PortfolioView data={data} saved={false} />);
    expect(screen.getByText("2 holdings have no price and aren't counted.")).toBeInTheDocument();
    // Value, Weight and P&L are "—"; Qty and Avg cost are still the file's.
    const cells = within(row("NIFTYBEES")).getAllByRole("cell");
    expect(cells.map((c) => c.textContent)).toEqual([expect.stringContaining("NIFTYBEES"), "Qty 10", "Avg cost 1,560.20", "—", "—", "—", "Score —"]);
  });

  it("says one holding without a price in the singular", () => {
    render(<PortfolioView data={{ ...fixturePortfolio(), unpriced: 1 }} saved={false} />);
    expect(screen.getByText("1 holding has no price and isn't counted.")).toBeInTheDocument();
  });

  it("shows the success banner only when saved", () => {
    const p = fixturePortfolio();
    const { unmount } = render(<PortfolioView data={p} saved={false} />);
    expect(screen.queryByText(/^Saved /)).toBeNull();
    unmount();
    const { unmount: again } = render(<PortfolioView data={p} saved />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved 23 holdings from kite_holdings.csv (replaced your portfolio)");
    again();
    render(<PortfolioView data={{ ...p, last_import: { ...p.last_import!, mode: "add", rows: 1 } }} saved />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved 1 holding from kite_holdings.csv (added to your portfolio)");
  });

  it("heads the page with the ratings and prices dates, and an import button", () => {
    const p = fixturePortfolio();
    const { unmount } = render(<PortfolioView data={p} saved={false} />);
    expect(screen.getByRole("heading", { level: 1, name: "Your portfolio" })).toBeInTheDocument();
    expect(screen.getByText("Ratings as of 24 Oct 2026 · Prices 30 Oct 2026")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import from Zerodha" })).toHaveAttribute("href", "/portfolio/import");
    unmount();
    render(<PortfolioView data={{ ...p, older_ratings: 3, prices_from: "2026-10-23" }} saved={false} />);
    expect(screen.getByText("Ratings as of 24 Oct 2026 · 3 holdings carry older ratings · Prices 23 Oct–30 Oct 2026")).toBeInTheDocument();
  });

  it("links matched symbols to their stock page and leaves unmatched ones plain", () => {
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    expect(screen.getByRole("link", { name: "TMCV" })).toHaveAttribute("href", "/stocks/TMCV");
    expect(screen.getByRole("link", { name: "SBIN" })).toHaveAttribute("href", "/stocks/SBIN");
    expect(screen.queryByRole("link", { name: "NIFTYBEES" })).toBeNull();
    expect(screen.getByText("NIFTYBEES", { selector: "b" })).toBeInTheDocument();
    const tmcv = within(row("TMCV")).getAllByRole("cell").map((c) => c.textContent);
    expect(tmcv.slice(1)).toEqual(["Qty 100", "Avg cost 1,073.67", "₹1,45,853.00", "10.3%", "+35.8%", "Score 79.1"]);
  });

  it("puts the not-rated reason under the name", () => {
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    expect(within(row("NIFTYBEES")).getByText(OUTSIDE_UNIVERSE)).toBeInTheDocument();
    expect(within(row("EMBASSY-RR")).getByText(OUTSIDE_UNIVERSE)).toBeInTheDocument();
    expect(within(row("SBIN")).getByText(/^The model can't read annual statements from banks'/)).toBeInTheDocument();
    expect(within(row("TMCV")).queryByText(OUTSIDE_UNIVERSE)).toBeNull();
  });

  it("shows a red-flag pill with the catalogue's label", () => {
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    const pill = within(row("BAJFINANCE")).getByTitle("CFO/PAT < 0.5 for 3 consecutive years");
    expect(pill).toHaveTextContent("Red flag: Cash conv.");
  });

  // HAND-EDITED ROWS: the captured portfolio has no holding whose rating
  // changed since a like-for-like run, none rated before the latest full run
  // and no split since the upload, so these copies of real rows set the flags.
  it("shows the was pill only when was_signal is set", () => {
    const data = edited({ TCS: { was_signal: "SELL" } });
    render(<PortfolioView data={data} saved={false} />);
    expect(within(row("TCS")).getByText("was SELL")).toBeInTheDocument();
    expect(screen.getAllByText(/^was /)).toHaveLength(1);
  });

  it("names an older rating's date, and an earlier model", () => {
    const data = edited(
      {
        TCS: { older_rating: true, earlier_model: true, signal_as_of: "2026-08-14" },
        MOTHERSON: { older_rating: true, earlier_model: false, signal_as_of: "2026-10-17" },
      },
      { older_ratings: 2 },
    );
    render(<PortfolioView data={data} saved={false} />);
    expect(within(row("TCS")).getByText("Rated 14 Aug 2026, earlier model")).toBeInTheDocument();
    expect(within(row("MOTHERSON")).getByText("Rated 17 Oct 2026")).toBeInTheDocument();
    expect(screen.getAllByText(/^Rated /)).toHaveLength(2);
    expect(screen.getByText(/2 holdings carry older ratings/)).toBeInTheDocument();
  });

  it("says when a split or bonus came after the upload", () => {
    render(<PortfolioView data={edited({ RELIANCE: { split_since_upload: true } })} saved={false} />);
    expect(within(row("RELIANCE")).getByText("Split or bonus since your upload. Import again to update")).toBeInTheDocument();
    expect(screen.getAllByText(/^Split or bonus/)).toHaveLength(1);
  });

  it("renders the empty state for no holdings", () => {
    const p = fixturePortfolio();
    const empty: Portfolio = {
      ...p,
      holdings: [],
      groups: p.groups.map((g) => ({ ...g, holdings: 0, value: 0, share: 0 })),
      value: 0,
      invested: 0,
      pnl: 0,
      pnl_pct: null,
      prices_from: null,
      prices_to: null,
      last_import: null,
    };
    render(<PortfolioView data={empty} saved={false} />);
    expect(screen.getByText("See your Zerodha holdings beside the model's latest rating of each stock.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import from Zerodha" })).toHaveAttribute("href", "/portfolio/import");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("Current value")).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete portfolio" })).toBeNull();
  });
});

describe("Delete portfolio", () => {
  it("asks first, then deletes and refreshes", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    fetchMock.mockResolvedValue(Response.json({ deleted: 23 }));
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    await user.click(screen.getByRole("button", { name: "Delete portfolio" }));
    expect(confirm).toHaveBeenCalledWith("Delete your saved portfolio? This can't be undone.");
    expect(fetchMock).toHaveBeenCalledWith("/api/portfolio", { method: "DELETE" });
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });

  it("does nothing when the visitor cancels", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("confirm", vi.fn(() => false));
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    await user.click(screen.getByRole("button", { name: "Delete portfolio" }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.refresh).not.toHaveBeenCalled();
  });

  it("says so when the delete fails", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("confirm", vi.fn(() => true));
    fetchMock.mockResolvedValue(Response.json({ detail: "x" }, { status: 503 }));
    render(<PortfolioView data={fixturePortfolio()} saved={false} />);
    await user.click(screen.getByRole("button", { name: "Delete portfolio" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't delete your portfolio. Try again.");
    expect(mocks.refresh).not.toHaveBeenCalled();
  });
});

describe("wording", () => {
  it("never tells the visitor what to do", async () => {
    const user = userEvent.setup();
    const flagged = edited({
      TCS: { was_signal: "SELL", older_rating: true, earlier_model: true, signal_as_of: "2026-08-14" },
      RELIANCE: { split_since_upload: true },
    }, { unpriced: 1, older_ratings: 1 });
    const texts: string[] = [];
    const { container, unmount } = render(<PortfolioView data={flagged} saved />);
    texts.push(container.textContent ?? "");
    unmount();
    const empty = render(<PortfolioView data={{ ...flagged, holdings: [] }} saved={false} />);
    texts.push(empty.container.textContent ?? "");
    empty.unmount();
    const form = render(<ImportForm hasHoldings />);
    texts.push(form.container.textContent ?? "");
    form.unmount();
    vi.mocked(authClient.useSession).mockReturnValue({ data: { user: { name: "A", email: "a@example.test" } }, isPending: false } as never);
    const menu = render(<AccountMenu enabled />);
    await user.click(screen.getByRole("button", { name: "Account" }));
    texts.push(menu.container.textContent ?? "");

    expect(texts.every((t) => t.length > 0)).toBe(true);
    for (const t of texts) expect(t).not.toMatch(ADVICE);
  });

  it("has no advice wording in the portfolio and account sources", () => {
    const root = join(process.cwd(), "src", "components");
    const files = [
      ...readdirSync(join(root, "portfolio")).map((f) => join(root, "portfolio", f)),
      join(root, "account", "AccountMenu.tsx"),
      join(process.cwd(), "src", "app", "portfolio", "page.tsx"),
    ];
    expect(files.length).toBeGreaterThan(5);
    for (const f of files) expect(readFileSync(f, "utf8")).not.toMatch(ADVICE);
  });
});

describe("the portfolio page", () => {
  const page = (saved?: string) => PortfolioPage({ params: Promise.resolve({}), searchParams: Promise.resolve(saved ? { saved } : {}) } as never);

  it("sends a signed-out visitor to sign in, then back", async () => {
    mocks.userGet.mockRejectedValue(new SignedOutError());
    await expect(page()).rejects.toThrow("NEXT_REDIRECT /login?next=/portfolio");
    expect(mocks.redirect).toHaveBeenCalledWith("/login?next=/portfolio");
  });

  it("lets an outage reach the error page rather than show an empty portfolio", async () => {
    mocks.userGet.mockRejectedValue(new ApiUnavailableError(503, "/me/portfolio"));
    await expect(page()).rejects.toBeInstanceOf(ApiUnavailableError);
    mocks.userGet.mockResolvedValue(null);
    await expect(page()).rejects.toBeInstanceOf(ApiUnavailableError);
    expect(mocks.redirect).not.toHaveBeenCalled();
  });

  it("is not there when accounts are off", async () => {
    mocks.accountsEnabled.mockReturnValue(false);
    await expect(page()).rejects.toThrow("NEXT_NOT_FOUND");
    expect(mocks.userGet).not.toHaveBeenCalled();
  });

  it("reads the visitor's portfolio and shows the banner only for the latest import", async () => {
    const p = fixturePortfolio();
    mocks.userGet.mockResolvedValue(p);
    const { unmount } = render(await page(String(p.last_import!.id)));
    expect(mocks.userGet).toHaveBeenCalledWith("/me/portfolio");
    expect(screen.getByText("Saved 23 holdings from kite_holdings.csv (replaced your portfolio)")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Portfolio" })[0]).toHaveAttribute("aria-current", "page");
    unmount();
    render(await page(String(p.last_import!.id + 1)));
    expect(screen.queryByText(/^Saved /)).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Your portfolio" })).toBeInTheDocument();
  });
});
