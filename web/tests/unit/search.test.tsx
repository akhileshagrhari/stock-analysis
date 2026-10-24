import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Instrument } from "@/lib/api/types";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api/client", async (orig) => ({ ...(await orig<typeof import("@/lib/api/client")>()), browserGet: vi.fn() }));

import { browserGet } from "@/lib/api/client";
import { SearchBox } from "@/components/shell/SearchBox";

const RELIANCE: Instrument = { isin: "INE002A01018", nse_symbol: "RELIANCE", name: "Reliance Industries Ltd.", sector: "Oil Gas & Consumable Fuels", bse_code: null, listing_date: null };
const RELAXO: Instrument = { ...RELIANCE, isin: "INE131B01039", nse_symbol: "RELAXO", name: "Relaxo Footwears Ltd.", sector: "Consumer Durables" };

const get = vi.mocked(browserGet);
const input = () => screen.getByRole("combobox");
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
let user: ReturnType<typeof userEvent.setup>;

function stubSearch(results: Record<string, Instrument[]>) {
  get.mockImplementation(async (path: string) => {
    const q = new URLSearchParams(path.split("?")[1]).get("q") ?? "";
    return (results[q] ?? []) as never;
  });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: false });
  user = userEvent.setup({ delay: null });
  get.mockReset();
  push.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("SearchBox", () => {
  it("does not query for whitespace", async () => {
    render(<SearchBox />);
    await user.type(input(), "   ");
    await advance(200);
    expect(get).not.toHaveBeenCalled();
  });

  it("debounces ~150ms and lists symbol, name, sector", async () => {
    stubSearch({ reli: [RELIANCE] });
    render(<SearchBox />);
    await user.type(input(), "reli");
    expect(get).not.toHaveBeenCalled();
    await advance(160);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get.mock.calls[0][0]).toBe("/instruments/search?q=reli&limit=8");
    const option = await screen.findByRole("option", { name: /RELIANCE.*Reliance Industries Ltd\..*Oil Gas/ });
    expect(option).toBeVisible();
  });

  it("ignores a stale response that resolves after a newer one", async () => {
    const pending: Record<string, (v: Instrument[]) => void> = {};
    get.mockImplementation((path: string) => new Promise((resolve) => {
      pending[new URLSearchParams(path.split("?")[1]).get("q")!] = resolve as (v: Instrument[]) => void;
    }));
    render(<SearchBox />);
    await user.type(input(), "rel");
    await advance(160);
    await user.type(input(), "i");
    await advance(160);
    await act(async () => pending["reli"]([RELIANCE]));
    await act(async () => pending["rel"]([RELAXO]));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([expect.stringContaining("RELIANCE")]);
  });

  it("does not report an outage for a request it cancelled itself", async () => {
    // Like fetch: an aborted request rejects with an AbortError.
    get.mockImplementation((_path: string, signal?: AbortSignal) => new Promise((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    render(<SearchBox />);
    await user.type(input(), "rel");
    await advance(160);
    await user.type(input(), "i");
    await advance(0);
    expect(screen.queryByText(/unavailable/i)).toBeNull();
  });

  it("ArrowDown + Enter navigates to /stocks/RELIANCE", async () => {
    stubSearch({ reli: [RELIANCE, RELAXO] });
    render(<SearchBox />);
    await user.type(input(), "reli");
    await advance(160);
    await screen.findAllByRole("option");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(push).toHaveBeenCalledWith("/stocks/RELIANCE");
  });

  it("encodes symbols with special characters", async () => {
    stubSearch({ "m&m": [{ ...RELIANCE, nse_symbol: "M&M", name: "Mahindra & Mahindra Ltd." }] });
    render(<SearchBox />);
    await user.type(input(), "m&m");
    await advance(160);
    expect(get.mock.calls[0][0]).toBe("/instruments/search?q=m%26m&limit=8");
    await screen.findAllByRole("option");
    await user.keyboard("{Enter}");
    expect(push).toHaveBeenCalledWith("/stocks/M%26M");
  });

  it("Escape closes the list", async () => {
    stubSearch({ reli: [RELIANCE] });
    render(<SearchBox />);
    await user.type(input(), "reli");
    await advance(160);
    await screen.findAllByRole("option");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input()).toHaveAttribute("aria-expanded", "false");
  });

  it("says so when nothing matches", async () => {
    stubSearch({});
    render(<SearchBox />);
    await user.type(input(), "zzz");
    await advance(160);
    expect(await screen.findByText("No NIFTY100 company matches “zzz”")).toBeInTheDocument();
  });

  it("⌘K focuses the bar search", async () => {
    render(<SearchBox size="bar" />);
    expect(input()).not.toHaveFocus();
    await user.keyboard("{Meta>}k{/Meta}");
    expect(input()).toHaveFocus();
  });
});
