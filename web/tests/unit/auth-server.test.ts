// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Better Auth and pg are replaced, so these tests see exactly when the site
// would start Better Auth or open a pool. The fake's `$context` is what
// Better Auth's own startup returns: a promise nobody awaits until a request
// arrives, which in production without a secret rejects.
const mocks = vi.hoisted(() => {
  const handler = vi.fn<(request: Request) => Promise<Response>>(async () => new Response("from better auth"));
  const getToken = vi.fn();
  const getSession = vi.fn();
  const fakeAuth = (context: Promise<unknown>) => ({
    handler,
    api: { getToken, getSession },
    options: {},
    $context: context,
  });
  return { handler, getToken, getSession, fakeAuth, betterAuth: vi.fn(), Pool: vi.fn() };
});
vi.mock("better-auth", () => ({ betterAuth: mocks.betterAuth }));
vi.mock("pg", () => ({ Pool: mocks.Pool }));
vi.mock("next/headers", () => ({ headers: async () => new Headers({ cookie: "better-auth.session_token=s" }) }));

const rejections: unknown[] = [];
const onRejection = (reason: unknown) => void rejections.push(reason);

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  rejections.length = 0;
  process.on("unhandledRejection", onRejection);
});
afterEach(() => {
  process.off("unhandledRejection", onRejection);
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function accountsOff() {
  vi.stubEnv("DATABASE_URL", undefined);
  vi.stubEnv("BETTER_AUTH_SECRET", undefined);
}

function accountsOn() {
  vi.stubEnv("DATABASE_URL", "postgresql:///stockanalysis_test");
  vi.stubEnv("BETTER_AUTH_SECRET", "a-test-secret-that-is-at-least-32-chars");
  mocks.betterAuth.mockImplementation(() => mocks.fakeAuth(Promise.resolve({})));
}

/** Lets a rejected promise that nobody handled reach `unhandledRejection`. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

describe("with accounts off", () => {
  it("importing the auth module builds nothing and leaves no rejection behind", async () => {
    accountsOff();
    mocks.betterAuth.mockImplementation(() =>
      mocks.fakeAuth(Promise.reject(new Error("You are using the default secret."))),
    );

    const server = await import("@/lib/auth/server");
    await import("@/app/api/auth/[...all]/route");
    await settle();

    expect(server.auth).toBeNull();
    expect(mocks.betterAuth).not.toHaveBeenCalled();
    expect(mocks.Pool).not.toHaveBeenCalled();
    expect(rejections).toEqual([]);
  });

  it("the /api/auth route answers 404 without touching Better Auth", async () => {
    accountsOff();
    const { GET, POST } = await import("@/app/api/auth/[...all]/route");

    const get = await GET(new Request("http://localhost:3000/api/auth/jwks"));
    const post = await POST(
      new Request("http://localhost:3000/api/auth/sign-in/email-otp", { method: "POST", body: "{}" }),
    );

    for (const res of [get, post]) {
      expect(res.status).toBe(404);
      expect(await res.json()).toEqual({ detail: "Not Found" });
    }
    expect(mocks.betterAuth).not.toHaveBeenCalled();
    expect(mocks.handler).not.toHaveBeenCalled();
  });

  it("nobody is signed in and there is no token", async () => {
    accountsOff();
    const { apiToken, currentUser } = await import("@/lib/auth/server");

    expect(await currentUser()).toBeNull();
    expect(await apiToken(new Headers({ cookie: "better-auth.session_token=s" }))).toBeNull();
    expect(mocks.getSession).not.toHaveBeenCalled();
    expect(mocks.getToken).not.toHaveBeenCalled();
  });
});

describe("with accounts on", () => {
  it("builds Better Auth once, on the auth schema, and the route forwards to it", async () => {
    accountsOn();
    const { GET, POST } = await import("@/app/api/auth/[...all]/route");

    expect(await (await GET(new Request("http://localhost:3000/api/auth/jwks"))).text()).toBe("from better auth");
    const post = await POST(new Request("http://localhost:3000/api/auth/sign-out", { method: "POST" }));
    expect(post.status).toBe(200);

    expect(mocks.betterAuth).toHaveBeenCalledOnce();
    expect(mocks.Pool).toHaveBeenCalledWith({
      connectionString: "postgresql:///stockanalysis_test",
      options: "-c search_path=auth",
    });
    expect(mocks.handler).toHaveBeenCalledTimes(2);
  });

  it("apiToken is null without a session, the token with one, and rethrows anything else", async () => {
    accountsOn();
    const { apiToken } = await import("@/lib/auth/server");

    mocks.getToken.mockRejectedValueOnce(
      Object.assign(new Error("Unauthorized"), { name: "APIError", statusCode: 401 }),
    );
    expect(await apiToken(new Headers())).toBeNull();

    mocks.getToken.mockResolvedValueOnce({ token: "header.payload.signature" });
    expect(await apiToken(new Headers({ cookie: "better-auth.session_token=s" }))).toBe("header.payload.signature");

    mocks.getToken.mockRejectedValueOnce(new Error("connection refused"));
    await expect(apiToken(new Headers())).rejects.toThrow("connection refused");
  });

  it("currentUser is the session's user, trimmed to id, email and name", async () => {
    accountsOn();
    const { currentUser } = await import("@/lib/auth/server");

    mocks.getSession.mockResolvedValueOnce(null);
    expect(await currentUser()).toBeNull();

    mocks.getSession.mockResolvedValueOnce({
      user: { id: "u1", email: "asha@example.test", name: "Asha", emailVerified: true, image: null },
      session: { id: "s1" },
    });
    expect(await currentUser()).toEqual({ id: "u1", email: "asha@example.test", name: "Asha" });
  });
});

// What `server.ts` handed Better Auth. The plugins are the real ones; only
// betterAuth itself is faked.
type Plugin = { id: string; options?: Record<string, unknown> };
type Options = {
  plugins: Plugin[];
  user: { deleteUser: { enabled: boolean; beforeDelete: (user: { id: string }, request?: Request) => Promise<void> } };
};
const given = () => mocks.betterAuth.mock.calls[0][0] as Options;
const plugin = (id: string) => given().plugins.find((p) => p.id === id);

describe("the Better Auth options", () => {
  it("never puts the API's token in a response header", async () => {
    accountsOn();
    await import("@/lib/auth/server");
    expect(plugin("jwt")?.options).toMatchObject({ disableSettingJwtHeader: true });
  });

  it("stores email codes hashed, and leaves code generation to Better Auth without a fixed code", async () => {
    accountsOn();
    vi.stubEnv("AUTH_DEV_FIXED_OTP", undefined);
    await import("@/lib/auth/server");
    const options = plugin("email-otp")?.options;
    expect(options?.storeOTP).toBe("hashed");
    // An explicit `generateOTP: undefined` replaces Better Auth's own generator
    // and every send fails, so the key must be absent.
    expect(options).not.toHaveProperty("generateOTP");
  });

  it("uses the fixed code outside production when one is set", async () => {
    accountsOn();
    vi.stubEnv("AUTH_DEV_FIXED_OTP", "246810");
    await import("@/lib/auth/server");
    const generate = plugin("email-otp")?.options?.generateOTP as () => string;
    expect(generate()).toBe("246810");
  });

  it.each([
    ["RESEND_API_KEY", { RESEND_API_KEY: undefined, EMAIL_FROM: "Stock Analysis <codes@example.test>" }],
    ["EMAIL_FROM", { RESEND_API_KEY: "re_test_key", EMAIL_FROM: undefined }],
  ])("leaves email codes out in production without %s, and says so once", async (_, env) => {
    accountsOn();
    vi.stubEnv("NODE_ENV", "production");
    for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value);
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    await import("@/lib/auth/server");
    expect(plugin("email-otp")).toBeUndefined();
    expect(plugin("jwt")).toBeDefined();
    expect(error).toHaveBeenCalledOnce();
    expect(error.mock.calls[0].join(" ")).toMatch(/RESEND_API_KEY and EMAIL_FROM/);
  });

  it("leaves email codes out quietly while `next build` collects pages", async () => {
    accountsOn();
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PHASE", "phase-production-build");
    vi.stubEnv("RESEND_API_KEY", undefined);
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    await import("@/lib/auth/server");
    expect(plugin("email-otp")).toBeUndefined();
    expect(error).not.toHaveBeenCalled();
  });

  it("offers email codes in production with an email service", async () => {
    accountsOn();
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("RESEND_API_KEY", "re_test_key");
    vi.stubEnv("EMAIL_FROM", "Stock Analysis <codes@example.test>");
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    await import("@/lib/auth/server");
    expect(plugin("email-otp")).toBeDefined();
    expect(error).not.toHaveBeenCalled();
  });
});

describe("deleting an account", () => {
  const user = { id: "u1" };
  const request = () =>
    new Request("http://localhost:3000/api/auth/delete-user", {
      method: "POST",
      headers: { cookie: "better-auth.session_token=s" },
    });

  async function beforeDelete(apiReply: () => Promise<Response>) {
    accountsOn();
    vi.stubEnv("SA_API_URL", "http://api.test:8000");
    const fetchMock = vi.fn<typeof fetch>(apiReply);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});
    await import("@/lib/auth/server");
    expect(given().user.deleteUser.enabled).toBe(true);
    return { hook: given().user.deleteUser.beforeDelete, fetchMock };
  }

  it("deletes the portfolio through the API first, with the visitor's own token", async () => {
    mocks.getToken.mockResolvedValue({ token: "tok" });
    const { hook, fetchMock } = await beforeDelete(async () => Response.json({ deleted: 3 }));

    await hook(user, request());

    const tokenFor = mocks.getToken.mock.calls[0][0] as { headers: Headers };
    expect(tokenFor.headers.get("cookie")).toBe("better-auth.session_token=s");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test:8000/v1/me/portfolio");
    expect(init?.method).toBe("DELETE");
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer tok");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it.each([
    ["the API refuses", async () => Response.json({ detail: "Portfolio needs Postgres." }, { status: 503 })],
    ["the API can't be reached", async () => Promise.reject(new TypeError("fetch failed"))],
  ])("keeps the account when %s", async (_, reply) => {
    mocks.getToken.mockResolvedValue({ token: "tok" });
    const { hook } = await beforeDelete(reply);
    await expect(hook(user, request())).rejects.toMatchObject({ name: "APIError", body: { code: "PORTFOLIO_NOT_DELETED" } });
  });

  it("keeps the account when no token can be minted, or there is no request to mint it from", async () => {
    mocks.getToken.mockRejectedValue(Object.assign(new Error("Unauthorized"), { name: "APIError", statusCode: 401 }));
    const { hook, fetchMock } = await beforeDelete(async () => Response.json({ deleted: 0 }));
    await expect(hook(user, request())).rejects.toMatchObject({ body: { code: "PORTFOLIO_NOT_DELETED" } });
    await expect(hook(user, undefined)).rejects.toMatchObject({ body: { code: "PORTFOLIO_NOT_DELETED" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
