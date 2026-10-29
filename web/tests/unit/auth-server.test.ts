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
