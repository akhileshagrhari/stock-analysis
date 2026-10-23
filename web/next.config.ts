import type { NextConfig } from "next";

const apiUrl = process.env.SA_API_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The browser calls /api/v1/*; Next forwards it to the API, so the API's
  // origin never needs CORS.
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${apiUrl}/v1/:path*` }];
  },
};

export default nextConfig;
