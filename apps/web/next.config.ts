import type { NextConfig } from 'next';

const agentApiOrigin = process.env.AGENT_API_ORIGIN ?? 'http://127.0.0.1:8002';

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${agentApiOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
