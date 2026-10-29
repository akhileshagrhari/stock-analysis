import { toNextJsHandler } from "better-auth/next-js";

import { auth } from "@/lib/auth/server";

// Better Auth's own endpoints: sign-in, sign-out, the session, the JWT the API
// accepts, and the JWKS (`/api/auth/jwks`) the API verifies it with.
export const { GET, POST } = toNextJsHandler(auth);
