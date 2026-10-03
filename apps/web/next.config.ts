import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The local API listens on 127.0.0.1 only (CLAUDE.md security rule);
  // the browser/Electron renderer reaches it through this same-origin proxy,
  // keeping cookies first-party.
  async rewrites() {
    const apiBase = process.env.PHARMAOS_API_INTERNAL_URL ?? 'http://127.0.0.1:8000';
    return [{ source: '/api/v1/:path*', destination: `${apiBase}/api/v1/:path*` }];
  },
};

export default nextConfig;
