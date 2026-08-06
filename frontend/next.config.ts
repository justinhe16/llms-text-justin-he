import createMDX from "@next/mdx";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `md`/`mdx` join the defaults so `app/docs/page.mdx` is a route. Listing `ts` and `tsx`
  // is not optional once this key is set: it replaces the default list rather than
  // extending it, and omitting them would unroute every other page in the app.
  pageExtensions: ["ts", "tsx", "md", "mdx"],

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

// No remark or rehype plugins. `/docs` is one hand-written page of prose, and every element
// it uses is styled by the components map in `mdx-components.tsx` — a syntax-highlighting or
// heading-anchor plugin would be build weight bought for a page that has neither a code
// sample worth colouring nor a table of contents to link into.
const withMDX = createMDX({});

export default withMDX(nextConfig);
