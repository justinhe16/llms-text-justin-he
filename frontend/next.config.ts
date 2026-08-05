import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  images: {
    // GitHub avatars surfaced from user_metadata.avatar_url
    // (lib/auth/use-user.ts) and rendered via next/image in
    // components/auth/user-menu.tsx.
    remotePatterns: [
      {
        protocol: "https",
        hostname: "avatars.githubusercontent.com",
        pathname: "/**",
      },
    ],
  },
};

export default nextConfig;
