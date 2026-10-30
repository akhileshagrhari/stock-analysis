import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, refresh }) }));
vi.mock("@/lib/auth/client", () => ({
  authClient: {
    signIn: { social: vi.fn(), emailOtp: vi.fn() },
    emailOtp: { sendVerificationOtp: vi.fn() },
  },
}));

import { authClient } from "@/lib/auth/client";
import { LoginView } from "@/components/account/LoginView";
import { NavList } from "@/components/shell/NavList";

const send = vi.mocked(authClient.emailOtp.sendVerificationOtp);
const signIn = vi.mocked(authClient.signIn.emailOtp);

beforeEach(() => {
  vi.clearAllMocks();
  send.mockResolvedValue({ data: { success: true }, error: null } as never);
});

describe("LoginView", () => {
  it("offers Google only when it is set up", () => {
    const { rerender } = render(<LoginView google={false} emailCodes next="/portfolio" />);
    expect(screen.queryByRole("button", { name: "Continue with Google" })).toBeNull();
    rerender(<LoginView google emailCodes next="/portfolio" />);
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
  });

  it("sends a code to the email, then asks for it", async () => {
    const user = userEvent.setup();
    render(<LoginView google={false} emailCodes next="/portfolio" />);
    await user.type(screen.getByLabelText("Email"), "a@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));
    expect(send).toHaveBeenCalledWith({ email: "a@example.test", type: "sign-in" });
    expect(await screen.findByLabelText("Code")).toBeInTheDocument();
  });

  it("says a code is on its way without promising it arrived", async () => {
    const user = userEvent.setup();
    render(<LoginView google={false} emailCodes next="/portfolio" />);
    await user.type(screen.getByLabelText("Email"), "a@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/If it doesn't arrive in a minute or two, check your spam folder or send a new code\./)).toBeInTheDocument();
  });

  it("offers no email form when email codes are off", () => {
    const { rerender } = render(<LoginView google emailCodes={false} next="/portfolio" />);
    expect(screen.queryByLabelText("Email")).toBeNull();
    expect(screen.queryByRole("button", { name: "Send code" })).toBeNull();
    expect(screen.queryByText("or")).toBeNull();
    expect(screen.getByText("Email sign-in isn't available right now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    rerender(<LoginView google={false} emailCodes={false} next="/portfolio" />);
    expect(screen.getByText("Email sign-in isn't available right now.")).toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
  });

  it("says so when the code is wrong", async () => {
    const user = userEvent.setup();
    signIn.mockResolvedValue({ data: null, error: { message: "Invalid OTP" } } as never);
    render(<LoginView google={false} emailCodes next="/portfolio" />);
    await user.type(screen.getByLabelText("Email"), "a@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));
    await user.type(await screen.findByLabelText("Code"), "000000");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("That code didn't work. Check it or send a new one.")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("goes to next after a good code", async () => {
    const user = userEvent.setup();
    signIn.mockResolvedValue({ data: { token: "t" }, error: null } as never);
    render(<LoginView google={false} emailCodes next="/signals" />);
    await user.type(screen.getByLabelText("Email"), "a@example.test");
    await user.click(screen.getByRole("button", { name: "Send code" }));
    await user.type(await screen.findByLabelText("Code"), "246810");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(signIn).toHaveBeenCalledWith({ email: "a@example.test", otp: "246810" });
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith("/signals"));
  });
});

describe("NavList", () => {
  it("shows Portfolio only when accounts are on", () => {
    const { rerender } = render(<NavList accounts={false} />);
    expect(screen.queryByRole("link", { name: "Portfolio" })).toBeNull();
    rerender(<NavList accounts />);
    expect(screen.getByRole("link", { name: "Portfolio" })).toHaveAttribute("href", "/portfolio");
  });
});
