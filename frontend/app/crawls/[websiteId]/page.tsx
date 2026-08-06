import { Suspense } from "react";
import type { Metadata } from "next";

import { Skeleton } from "@/components/ui/skeleton";
import { CrawlsHeader } from "@/components/crawls/crawls-header";
import { WebsiteDetail } from "@/components/crawls/website-detail";

export const metadata: Metadata = {
  title: "Crawl · llms-text",
};

/**
 * `/crawls/[websiteId]` — one website's detail page.
 *
 * A server component that does almost nothing: it awaits `params` (a Promise in the App
 * Router) and hands the id to the client component that owns the page. There is no data
 * fetching here on purpose — every read on this page goes through React Query in the
 * browser, against the same-origin BFF proxy (ARCHITECTURE.md §8.1), and prefetching one of
 * them server-side would mean a second code path to the same endpoint plus a hydration
 * boundary to keep in sync with it.
 *
 * The `<Suspense>` boundary is not optional. `WebsiteDetail` reads the tab and the selected
 * run from `useSearchParams`, and Next requires any component that does so to sit inside a
 * boundary — without one, `next build` fails the route rather than warning about it.
 * `app/page.tsx` has the same wrapping around its own `useSearchParams` consumers.
 *
 * Access is enforced in `frontend/middleware.ts`, which redirects an unauthenticated
 * request for anything under `/crawls` to `/?next=...`. This page does not re-check.
 */
export default async function WebsiteDetailPage({
  params,
}: {
  params: Promise<{ websiteId: string }>;
}) {
  const { websiteId } = await params;

  return (
    <div className="flex min-h-screen flex-col">
      {/* The same chrome `/crawls` renders (PER-161). Shared rather than rebuilt, so the
          wordmark, "Add site" and the user menu cannot drift between the two screens. */}
      <CrawlsHeader />

      <main className="min-w-0 flex-1">
        <Suspense fallback={<DetailFallback />}>
          <WebsiteDetail websiteId={websiteId} />
        </Suspense>
      </main>
    </div>
  );
}

/** Matches the real page's frame — the same max width and padding — so the transition from
 * fallback to content does not jump the layout. */
function DetailFallback() {
  return (
    <div aria-hidden="true" className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">
      <Skeleton className="h-5 w-24" />
      <Skeleton className="mt-6 h-7 w-64" />
      <Skeleton className="mt-2 h-4 w-40" />
      <Skeleton className="mt-8 h-8 w-full" />
      <Skeleton className="mt-6 h-64 w-full" />
    </div>
  );
}
