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

// One rehype plugin. `rehype-slug` gives every heading in `app/docs/page.mdx` a
// deterministic `id` derived from its text by `github-slugger`, so `## Discovery` renders as
// `<h2 id="discovery">`. The diagram in the left column of `/docs` scrolls to those ids, and
// `lib/docs/sections.ts` names them; deriving the slug in `mdx-components.tsx` instead would
// mean reimplementing that slugger and keeping it in step with nothing.
//
// Still no syntax highlighting, and still no heading-anchor plugin: this page has neither a
// code sample worth colouring nor a table of contents to link into. The diagram is the table
// of contents.
//
// Named as a STRING rather than imported. Next transpiles this config to CommonJS before it
// loads it, and `rehype-slug` is ESM-only ("type": "module") — so an `import` here becomes a
// `require()` of an ESM module, which only works through Node's `require(esm)` bridge on
// Node >= 20.19. CI pins `node-version: 20`, so that would be a build that passes on a
// laptop and depends on the runner's patch version. `@next/mdx` resolves the string with a
// real dynamic `import()` inside webpack instead, and types it (`string | [name, options] |
// Pluggable`), so the risk goes away rather than being bet on.
const withMDX = createMDX({
  options: {
    rehypePlugins: ["rehype-slug"],
  },
});

export default withMDX(nextConfig);
