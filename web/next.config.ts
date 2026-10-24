import type { NextConfig } from "next";

// The browser reaches the API through src/app/api/v1/[...path]/route.ts,
// which reads SA_API_URL at request time.
//
// Security headers for every route. The CSP restricts what can embed or be
// embedded and leaves scripts alone: Next's inline hydration scripts would
// need per-request nonces for a script-src policy. HSTS belongs to whatever
// terminates TLS in front of the site.
const SECURITY_HEADERS = [
  { key: "Content-Security-Policy", value: "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
