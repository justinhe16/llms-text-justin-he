import type { Metadata } from "next";

import { CrawlsHeader } from "@/components/crawls/crawls-header";
import { CrawlsView } from "@/components/crawls/crawls-view";

export const metadata: Metadata = {
  title: "Crawls · llms-text",
  description: "Every website registered for crawling, and the state of its latest run.",
};

/**
 * `/crawls` — every website in the system, one row each.
 *
 * Every signed-in user sees every website here, regardless of who added it. That is
 * ARCHITECTURE.md §4.1's read rule showing through to the UI, not an oversight: this
 * application is not multi-tenant, `GET /websites` does not filter by `user_id`, and the
 * Owner column exists precisely because the list is shared. Ownership decides what you can
 * *change*, on the detail page; it decides nothing about what you can see.
 *
 * No auth check in this component. `frontend/middleware.ts` gates `/crawls` server-side
 * before anything here renders, which is the only place route protection is allowed to live
 * (ARCHITECTURE.md §8.2) — a second check here would be both redundant and, being
 * client-visible, not actually a check.
 *
 * A server component holding two client islands. It fetches nothing itself: the table's data
 * arrives through React Query in the browser (see `CrawlsView`), which is what lets the same
 * query poll, dedupe and cache without this page re-rendering on the server every three
 * seconds.
 */
export default function CrawlsPage() {
  return (
    <div className="flex min-h-screen flex-col">
      <CrawlsHeader />

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">
        <h1 className="mb-6 text-xl font-medium tracking-tight text-foreground">Crawls</h1>
        <CrawlsView />
      </main>
    </div>
  );
}
