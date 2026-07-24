import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",           // static export → served by FastAPI in production
  trailingSlash: true,
  async rewrites() {
    // In dev, proxy /api/* to the FastAPI backend
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
