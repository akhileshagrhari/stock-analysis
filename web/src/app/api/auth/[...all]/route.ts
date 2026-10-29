import { toNextJsHandler } from "better-auth/next-js";

import { auth } from "@/lib/auth/server";

// Better Auth's own endpoints: sign-in, sign-out, the session, the JWT the API
// accepts, and the JWKS (`/api/auth/jwks`) the API verifies it with.
//
// With accounts off there is no Better Auth behind them, so every path is a
// 404, the same answer as for a route that was never deployed.

const handlers = auth ? toNextJsHandler(auth) : null;

const notFound = () => Response.json({ detail: "Not Found" }, { status: 404 });

export function GET(request: Request): Promise<Response> | Response {
  return handlers ? handlers.GET(request) : notFound();
}

export function POST(request: Request): Promise<Response> | Response {
  return handlers ? handlers.POST(request) : notFound();
}
