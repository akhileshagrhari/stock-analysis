import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }), usePathname: () => "/signals" }));
vi.mock("@/lib/auth/client", () => ({
  authClient: { useSession: vi.fn(), signOut: vi.fn() },
}));

import { authClient } from "@/lib/auth/client";
import { AccountMenu } from "@/components/account/AccountMenu";

const useSession = vi.mocked(authClient.useSession);
const session = (data: unknown, isPending = false) => useSession.mockReturnValue({ data, isPending } as never);

beforeEach(() => vi.clearAllMocks());

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
    expect(screen.queryByRole("button", { name: "Delete account" })).toBeNull();
  });

  it("prefers the name for the letter", () => {
    session({ user: { name: "alice", email: "zed@example.test" } });
    render(<AccountMenu enabled />);
    expect(screen.getByRole("button", { name: /Account/ })).toHaveTextContent("A");
  });
});
