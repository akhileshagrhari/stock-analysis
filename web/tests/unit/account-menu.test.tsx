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

  it("deletes the account with one request, which removes the portfolio first on the server, then goes home", async () => {
    deleteUser.mockResolvedValue({ data: { success: true }, error: null } as never);
    const confirm = await openAndDelete(true);
    expect(confirm).toHaveBeenCalledWith("Delete your account and portfolio? This can't be undone.");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/"));
    expect(deleteUser).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does nothing when the visitor cancels", async () => {
    await openAndDelete(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(deleteUser).not.toHaveBeenCalled();
  });

  it("asks a visitor whose sign-in is too old to sign in again first", async () => {
    deleteUser.mockResolvedValue({
      data: null,
      error: { code: "SESSION_EXPIRED", message: "Session expired. Re-authenticate to perform this action.", status: 400, statusText: "Bad Request" },
    } as never);
    await openAndDelete(true);
    expect(await screen.findByRole("alert")).toHaveTextContent("For your security, sign out and sign in again, then delete your account.");
    expect(push).not.toHaveBeenCalled();
  });

  it("says nothing was deleted when the portfolio could not be", async () => {
    deleteUser.mockResolvedValue({
      data: null,
      error: { code: "PORTFOLIO_NOT_DELETED", message: "x", status: 503, statusText: "Service Unavailable" },
    } as never);
    await openAndDelete(true);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't delete your portfolio, so nothing was deleted. Try again later.");
    expect(push).not.toHaveBeenCalled();
  });

  it("says so when the account could not be deleted for any other reason", async () => {
    deleteUser.mockRejectedValue(new TypeError("Failed to fetch"));
    await openAndDelete(true);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't delete your account. Try again later.");
    expect(push).not.toHaveBeenCalled();
  });

  it("prefers the name for the letter", () => {
    session({ user: { name: "alice", email: "zed@example.test" } });
    render(<AccountMenu enabled />);
    expect(screen.getByRole("button", { name: /Account/ })).toHaveTextContent("A");
  });
});
