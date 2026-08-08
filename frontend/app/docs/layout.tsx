import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { DocsTabs } from "@/components/docs/docs-tabs";

/**
 * The frame shared by all three `/docs` tabs: the back link and the tab nav. No `metadata`
 * export here — each tab (`app/docs/(run)/layout.tsx`, `app/docs/architecture/layout.tsx`,
 * `app/docs/features/layout.tsx`) owns its own title and description, and this file wraps
 * all three, so root `app/layout.tsx:24-27`'s fallback title covers this frame if any tab's
 * metadata is ever removed.
 *
 * ## Three routes, not three client-side tabs
 *
 * `/docs`, `/docs/architecture` and `/docs/features` are ordinary Next routes rather than a
 * client component switching on state, for three reasons that all trace back to the URL
 * being the only thing that has to remember which tab a reader is on:
 *
 * - `/docs#fetch` is a published, deep-linkable anchor into the run tab. A client-side switch
 *   would need to restore both the active tab *and* the scroll position from a hash on every
 *   load; a route needs neither, because the browser already does both for a URL. This is
 *   also why Features is not the index route (`components/docs/docs-tabs.tsx` explains the
 *   display-order-vs-route split): `/docs` has to keep meaning the run tab for that anchor to
 *   keep resolving, whichever tab is drawn leftmost.
 * - There is no client state to restore on navigation, on refresh, or on the back button —
 *   the active tab is just whichever route rendered.
 * - Each tab owns its own left column (`DocsDiagram`, `ArchitectureDiagram` or
 *   `FeaturesContents`) inside its own route-group layout, so swapping tabs is an ordinary
 *   navigation between pages that happen to share this frame, not a re-render of one page
 *   pretending to be three.
 *
 * Public. `frontend/middleware.ts`'s `PROTECTED_PREFIXES` is `["/crawls"]` — `/docs` was
 * never in it, so all three tabs render for a signed-out visitor exactly as they do for a
 * signed-in one — documentation nobody can read before signing up is documentation that
 * cannot do its job. `/docs/architecture` and `/docs/features` both inherit that exemption
 * for free, simply by never matching the one prefix the list does contain, so no middleware
 * change was needed to add either one.
 */
export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-6xl px-6 py-14 sm:py-20">
      <Link
        href="/"
        className="inline-flex items-center gap-1.5 rounded-md text-sm text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <ArrowLeft className="size-3.5" aria-hidden="true" />
        Back
      </Link>

      <DocsTabs />

      {children}
    </main>
  );
}
