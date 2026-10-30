import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, refresh }), usePathname: () => "/signals" }));
vi.mock("@/lib/auth/client", () => ({
  authClient: { useSession: vi.fn(), signOut: vi.fn(), deleteUser: vi.fn() },
}));

import { authClient } from "@/lib/auth/client";
import { AccountMenu } from "@/components/account/AccountMenu";

const useSession = vi.mocked(authClient.useSession);
const deleteUser = vi.mocked(authClient.deleteUser);
const fetchMock = vi.fn();
const session = (data: unknown, isPending = false) => useSession.mockReturnValue({ data, isPending } as never);

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
});

describe("AccountMenu", () => {
  it("renders nothing, and never asks for a session, when accounts are off", () => {
    const { container } = render(<AccountMenu enabled={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(useSession).not.toHaveBeenCalled();
  });

  it("links signed-out visitors to sign in, then back to the same page", () => {
    session(null);
    render(<AccountMenu enabled />);
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login?next=%2Fsignals");
  });

  it("shows the first letter and a menu when signed in", async () => {
    session({ user: { name: "", email: "zed@example.test" } });
    const user = userEvent.setup();
    render(<AccountMenu enabled />);
    const button = screen.getByRole("button", { name: /Account/ });
    expect(button).toHaveTextContent("Z");
    await user.click(button);
    expect(screen.getByRole("link", { name: "Portfolio" })).toHaveAttribute("href", "/portfolio");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete account" })).toBeInTheDocument();
  });

  async function openAndDelete(confirmed: boolean) {
    session({ user: { name: "", email: "zed@example.test" } });
    const confirm = vi.fn(() => confirmed);
    vi.stubGlobal("confirm", confirm);
    const user = userEvent.setup();
    render(<AccountMenu enabled />);
    await user.click(screen.getByRole("button", { name: /Account/ }));
    await user.click(screen.getByRole("button", { name: "Delete account" }));
    return confirm;
  }

  it("deletes the portfolio, then the account, then goes home", async () => {
    const order: string[] = [];
    fetchMock.mockImplementation(async () => {
      order.push("portfolio");
      return Response.json({ deleted: 3 });
    });
    deleteUser.mockImplementation((async () => {
      order.push("account");
      return { data: { success: true }, error: null };
    }) as never);
    const confirm = await openAndDelete(true);
    expect(confirm).toHaveBeenCalledWith("Delete your account and portfolio? This can't be undone.");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/"));
    expect(fetchMock).toHaveBeenCalledWith("/api/portfolio", { method: "DELETE" });
    expect(order).toEqual(["portfolio", "account"]);
  });

  it("does nothing when the visitor cancels", async () => {
    await openAndDelete(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(deleteUser).not.toHaveBeenCalled();
  });

  it("keeps the account when the portfolio could not be deleted", async () => {
    fetchMock.mockResolvedValue(Response.json({ detail: "x" }, { status: 503 }));
    await openAndDelete(true);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't delete your account. Try again.");
    expect(deleteUser).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("says so when the account itself could not be deleted", async () => {
    fetchMock.mockResolvedValue(Response.json({ deleted: 3 }));
    deleteUser.mockResolvedValue({ data: null, error: { message: "Session expired" } } as never);
    await openAndDelete(true);
    expect(await screen.findByRole("alert")).toHaveTextContent("Your portfolio is deleted, but your account isn't. Sign in again, then try once more.");
    expect(push).not.toHaveBeenCalled();
  });

  it("prefers the name for the letter", () => {
    session({ user: { name: "alice", email: "zed@example.test" } });
    render(<AccountMenu enabled />);
    expect(screen.getByRole("button", { name: /Account/ })).toHaveTextContent("A");
  });
});
