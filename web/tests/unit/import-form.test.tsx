import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, refresh: vi.fn() }) }));

import { ImportForm } from "@/components/portfolio/ImportForm";

const fetchMock = vi.fn();
const csv = () => new File(["a,b\n1,2\n"], "holdings.csv", { type: "text/csv" });
const reply = (status: number, body: unknown) => Response.json(body, { status });

async function choose(user: ReturnType<typeof userEvent.setup>) {
  await user.upload(screen.getByLabelText(/Drop your Kite holdings file here/), csv());
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
});

describe("ImportForm", () => {
  it("defaults to Replace with no holdings, Add with holdings", () => {
    const { unmount } = render(<ImportForm hasHoldings={false} />);
    expect(screen.getByRole("button", { name: "Replace my portfolio" })).toHaveAttribute("aria-pressed", "true");
    unmount();
    render(<ImportForm hasHoldings />);
    expect(screen.getByRole("button", { name: "Add to my portfolio" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps Save off until a file is chosen", async () => {
    const user = userEvent.setup();
    render(<ImportForm hasHoldings={false} />);
    const save = screen.getByRole("button", { name: "Save portfolio" });
    expect(save).toBeDisabled();
    await choose(user);
    expect(save).toBeEnabled();
  });

  it("sends one request for two quick clicks", async () => {
    const user = userEvent.setup();
    fetchMock.mockReturnValue(new Promise(() => {}));
    render(<ImportForm hasHoldings />);
    await choose(user);
    const save = screen.getByRole("button", { name: /Save portfolio|Saving/ });
    await user.dblClick(save);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(save).toBeDisabled();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/portfolio/import?mode=add&filename=holdings.csv");
    expect(init.method).toBe("POST");
  });

  it("shows a 422's message and problems", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue(reply(422, { detail: { message: "We couldn't read that file.", problems: ["Row 3: bad quantity"] } }));
    render(<ImportForm hasHoldings={false} />);
    await choose(user);
    await user.click(screen.getByRole("button", { name: "Save portfolio" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("We couldn't read that file.");
    expect(alert).toHaveTextContent("Row 3: bad quantity");
    expect(screen.getByRole("button", { name: "Save portfolio" })).toBeEnabled();
  });

  it("sends the browser to sign in on a 401", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue(reply(401, { detail: "Sign in again." }));
    render(<ImportForm hasHoldings={false} />);
    await choose(user);
    await user.click(screen.getByRole("button", { name: "Save portfolio" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login?next=/portfolio/import"));
  });

  it("says so when the server cannot be reached", async () => {
    const user = userEvent.setup();
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    render(<ImportForm hasHoldings={false} />);
    await choose(user);
    await user.click(screen.getByRole("button", { name: "Save portfolio" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't reach the server. Nothing was changed.");
  });

  it("goes to the portfolio on success", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue(reply(200, { id: 42, filename: "holdings.csv", mode: "replace", rows: 1, matched: 1, created_at: "x" }));
    render(<ImportForm hasHoldings={false} />);
    await choose(user);
    await user.click(screen.getByRole("button", { name: "Save portfolio" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/portfolio?saved=42"));
  });
});
