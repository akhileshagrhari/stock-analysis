import { afterEach, describe, expect, it, vi } from "vitest";

import { fixedOtp, sendSignInCode } from "@/lib/auth/email";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stubFetch(response: Response = new Response(JSON.stringify({ id: "e1" }), { status: 200 })) {
  const fetchMock = vi.fn<typeof fetch>(async () => response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("sendSignInCode", () => {
  it("logs the code in development when no email service is set", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("RESEND_API_KEY", undefined);
    vi.stubEnv("EMAIL_FROM", undefined);
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const fetchMock = stubFetch();

    await sendSignInCode("asha@example.test", "482913");

    expect(info).toHaveBeenCalledOnce();
    const logged = info.mock.calls[0].join(" ");
    expect(logged).toContain("asha@example.test");
    expect(logged).toContain("482913");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses in production without RESEND_API_KEY", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("RESEND_API_KEY", undefined);
    vi.stubEnv("EMAIL_FROM", "Stock Analysis <codes@example.test>");
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const fetchMock = stubFetch();

    await expect(sendSignInCode("asha@example.test", "482913")).rejects.toThrow(/email service is not configured/);
    expect(info).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses when Resend has a key but no sender", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("RESEND_API_KEY", "re_test_key");
    vi.stubEnv("EMAIL_FROM", undefined);
    const fetchMock = stubFetch();

    await expect(sendSignInCode("asha@example.test", "482913")).rejects.toThrow(/email service is not configured/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends through Resend when configured", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("RESEND_API_KEY", "re_test_key");
    vi.stubEnv("EMAIL_FROM", "Stock Analysis <codes@example.test>");
    const fetchMock = stubFetch();

    await sendSignInCode("asha@example.test", "482913");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.resend.com/emails");
    expect(init?.method).toBe("POST");
    const headers = new Headers(init?.headers);
    expect(headers.get("authorization")).toBe("Bearer re_test_key");
    expect(headers.get("content-type")).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual({
      from: "Stock Analysis <codes@example.test>",
      to: ["asha@example.test"],
      subject: "Your sign-in code",
      text: "Your code is 482913. It expires in 5 minutes.",
    });
  });

  it("fails when Resend refuses the message", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("RESEND_API_KEY", "re_test_key");
    vi.stubEnv("EMAIL_FROM", "Stock Analysis <codes@example.test>");
    stubFetch(new Response(JSON.stringify({ message: "domain not verified" }), { status: 403 }));

    const sending = sendSignInCode("asha@example.test", "482913");
    await expect(sending).rejects.toThrow(/403/);
    await expect(sending).rejects.not.toThrow(/re_test_key/);
  });
});

describe("fixedOtp", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("fixedOtp is honoured outside production only", () => {
    vi.stubEnv("AUTH_DEV_FIXED_OTP", "123456");
    vi.stubEnv("NODE_ENV", "development");
    expect(fixedOtp()).toBe("123456");
    vi.stubEnv("NODE_ENV", "test");
    expect(fixedOtp()).toBe("123456");
    vi.stubEnv("NODE_ENV", "production");
    expect(fixedOtp()).toBeNull();
  });

  it("is null when unset", () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("AUTH_DEV_FIXED_OTP", undefined);
    expect(fixedOtp()).toBeNull();
    vi.stubEnv("AUTH_DEV_FIXED_OTP", "");
    expect(fixedOtp()).toBeNull();
  });
});
