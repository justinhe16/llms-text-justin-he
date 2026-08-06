"use client";

import { ArrowLeftIcon, CalendarClockIcon, ChartLineIcon } from "lucide-react";
import Link from "next/link";

import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useUser } from "@/lib/auth/use-user";
import { useWebsite } from "@/lib/query/use-website";
import { flattenRunPages, useRunsInfinite } from "@/lib/query/use-runs-infinite";
import { anyRunActive } from "@/lib/api/run-status";
import { DETAIL_TABS, useDetailView, type DetailTab } from "@/lib/crawls/use-detail-view";

import { OutputTab } from "./output-tab";
import { OwnerLabel } from "./owner-label";
import { PlaceholderTab } from "./placeholder-tab";
import { RunNowButton } from "./run-now-button";
import { RunStatusDot, runStatusLabel, type RunStatusOrIdle } from "./run-status-indicator";
import { RunsTab } from "./runs-tab";

const TAB_LABELS: Record<DetailTab, string> = {
  runs: "Runs",
  output: "Output",
  schedule: "Schedule",
  trends: "Trends",
};

/**
 * `/crawls/[websiteId]` — the tabbed detail page.
 *
 * ## One run query for the whole page
 *
 * `useRunsInfinite` is called once, here, and its results are passed down. The header needs
 * "is a run active" for the Run now button, the Runs tab renders the rows, and the Output
 * tab needs the same list for its run picker and its default selection — three consumers of
 * one request. Calling the hook in each of them would work (React Query would dedupe the
 * identical key), but it would also mean three components independently deciding what
 * filters to fetch with, and the first one to disagree would silently start a second query.
 *
 * The one request this page does *not* hoist is `GET /runs/{id}`: that belongs to whichever
 * run is being displayed, and it lives inside the Output tab for exactly that reason.
 *
 * ## Tabs mount lazily and stay mounted
 *
 * Radix renders only the active tab's content by default, so the Output tab issues no
 * detail request until a user opens it — which is what keeps a page load from fetching an
 * artifact nobody asked to see.
 */
export function WebsiteDetail({ websiteId }: { websiteId: string }) {
  const { tab, selectedRunId, setTab, showRunOutput } = useDetailView();
  const { user } = useUser();

  const websiteQuery = useWebsite(websiteId);
  const runsQuery = useRunsInfinite(websiteId);

  const runs = flattenRunPages(runsQuery.data);
  const website = websiteQuery.data;

  // The header's status is the newest run's status. `runs` is newest-first from the API, so
  // this needs no sort — and a website with no runs at all is `idle`, a display-only value
  // that deliberately is not part of the `RunStatus` vocabulary.
  const headerStatus: RunStatusOrIdle = runs[0]?.status ?? "idle";

  // `anyRunActive` over the *first* page only: an active run is always among the newest,
  // because a run cannot become active again after finishing. Folding over every loaded
  // page would give the same answer at more cost. This is also the exact predicate that
  // drives polling, so the button and the refetch loop can never disagree about whether
  // something is running.
  const hasActiveRun = runsQuery.data?.pages[0] !== undefined
    ? anyRunActive(runsQuery.data.pages[0])
    : false;

  const isOwner = user !== null && website !== undefined && user.id === website.user_id;

  if (websiteQuery.isError) {
    return (
      <div className="mx-auto w-full max-w-5xl px-6 py-10">
        <BackLink />
        <p className="mt-6 rounded-lg border border-border bg-card p-6 text-sm text-status-failed">
          {websiteQuery.error.message}
        </p>
      </div>
    );
  }

  return (
    // `min-w-0` on the column that contains the viewer is what actually keeps the page body
    // from scrolling horizontally: without it a flex child refuses to shrink below its
    // content's intrinsic width, and a single long line in an llms.txt would widen the whole
    // page instead of scrolling inside its own container.
    <div className="mx-auto w-full min-w-0 max-w-5xl px-6 py-10">
      <BackLink />

      <header className="mt-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          {website === undefined ? (
            <div className="space-y-2">
              <Skeleton className="h-7 w-64" />
              <Skeleton className="h-4 w-40" />
            </div>
          ) : (
            <>
              {/* The origin in Geist Mono. It is a URL — the mono face is what stops
                  `rn` from reading as `m` in a hostname someone is checking. */}
              <h1 className="truncate font-mono text-xl font-medium tracking-tight text-foreground">
                {website.origin}
              </h1>
              {website.title !== null && (
                <p className="mt-1 truncate text-sm text-muted-foreground">{website.title}</p>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="inline-flex items-center gap-1.5 text-sm text-muted-foreground">
                  <RunStatusDot status={headerStatus} />
                  {runStatusLabel(headerStatus)}
                </span>
                <span aria-hidden className="text-muted-foreground/40">
                  ·
                </span>
                <OwnerLabel userId={website.user_id} />
              </div>
            </>
          )}
        </div>

        {website !== undefined && (
          <RunNowButton
            websiteId={websiteId}
            ownerUserId={website.user_id}
            isOwner={isOwner}
            hasActiveRun={hasActiveRun}
            onShowRun={showRunOutput}
          />
        )}
      </header>

      <Tabs
        value={tab}
        onValueChange={(value) => setTab(value as DetailTab)}
        className="mt-8 min-w-0"
      >
        <TabsList variant="line" className="w-full justify-start border-b border-border">
          {DETAIL_TABS.map((name) => (
            <TabsTrigger key={name} value={name} className="flex-none px-3">
              {TAB_LABELS[name]}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="runs" className="mt-6 min-w-0">
          <RunsTab
            runs={runs}
            isLoading={runsQuery.isPending}
            isError={runsQuery.isError}
            error={runsQuery.error}
            hasNextPage={runsQuery.hasNextPage}
            isFetchingNextPage={runsQuery.isFetchingNextPage}
            onLoadMore={() => void runsQuery.fetchNextPage()}
            onSelectRun={showRunOutput}
            canRun={isOwner}
          />
        </TabsContent>

        <TabsContent value="output" className="mt-6 min-w-0">
          <OutputTab
            origin={website?.origin ?? ""}
            runs={runs}
            isLoadingRuns={runsQuery.isPending}
            selectedRunId={selectedRunId}
            onSelectRun={showRunOutput}
            canRun={isOwner}
          />
        </TabsContent>

        {/* PER-168 replaces this element. Nothing else on the page needs to change. */}
        <TabsContent value="schedule" className="mt-6 min-w-0">
          <PlaceholderTab
            icon={CalendarClockIcon}
            title="Scheduled crawls"
            description="Set how often this site is re-crawled and see when the next run is due."
          />
        </TabsContent>

        {/* PER-169 replaces this element. */}
        <TabsContent value="trends" className="mt-6 min-w-0">
          <PlaceholderTab
            icon={ChartLineIcon}
            title="Trends"
            description="Pages crawled and run duration over time, so a site that is quietly growing or slowing down is visible."
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}

/** Back to the crawls list. A plain `<Link>`, so it pushes a history entry — leaving the
 * page is a real navigation, unlike switching tabs within it. */
function BackLink() {
  return (
    <Link
      href="/crawls"
      className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
    >
      <ArrowLeftIcon className="size-4" aria-hidden />
      All crawls
    </Link>
  );
}
