// The browser's side of sign-in, and the only auth module a client component
// may import. It talks to `/api/auth/*` on this same origin.

import { emailOTPClient } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";

export const authClient = createAuthClient({ plugins: [emailOTPClient()] });
