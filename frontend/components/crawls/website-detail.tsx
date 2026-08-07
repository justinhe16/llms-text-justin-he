"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";

import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { anyRunActive } from "@/lib/api/run-status";
import { useUser } from "@/lib/auth/use-user";
import { rowStatusFromRunStatus, type RowStatus } from "@/lib/crawls/row-status";
import { DETAIL_TABS, useDetailView, type DetailTab } from "@/lib/crawls/use-detail-view";
import { flattenRunPages, useRunsInfinite } from "@/lib/query/use-runs-infinite";
import { useWebsite } from "@/lib/query/use-website";

import { CrawlOwner } from "./crawl-owner";
import { CrawlsError } from "./crawls-error";
import { OutputTab } from "./output-tab";
import { RunNowButton } from "./run-now-button";
import { RunStatusIndicator } from "./run-status-indicator";
import { RunsTab } from "./runs-tab";
import { ScheduleTab } from "./schedule-tab";
import { TrendsTab } from "./trends-tab";

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
 * one request. Calling the hook in each of them would work (React Query dedupes an identical
 * key), but it would also mean three components independently deciding what to fetch with,
 * and the first one to disagree would quietly start a second query.
 *
 * The one request this page does *not* hoist is `GET /runs/{id}`: that belongs to whichever
 * run is being displayed, and it lives inside the Output tab for exactly that reason.
 *
 * ## Tabs mount lazily
 *
 * Radix renders only the active tab's content, so the Output tab issues no detail request
 * until someone opens it — which is what keeps a page load from fetching an artifact nobody
 * asked to see.
 */
export function WebsiteDetail({ websiteId }: { websiteId: string }) {
  const { tab, selectedRunId, statsWindow, setTab, showRunOutput, setStatsWindow } =
    useDetailView();
  const { user } = useUser();

  const websiteQuery = useWebsite(websiteId);
  const runsQuery = useRunsInfinite(websiteId);

  const runs = flattenRunPages(runsQuery.data);
  const website = websiteQuery.data;

  // The header's status is the newest run's. `runs` is newest-first from the API, so this
  // needs no sort — and a website with no runs at all is `never-run`, the vocabulary
  // lib/crawls/row-status.ts owns for exactly that case.
  const headerStatus: RowStatus =
    runs[0] !== undefined ? rowStatusFromRunStatus(runs[0].status) : "never-run";

  // `anyRunActive` over the *first* page only. An active run is always among the newest —
  // a run cannot become active again after finishing — so folding over every loaded page
  // would give the same answer at more cost. This is also the exact predicate that drives
  // polling, so the button and the refetch loop can never disagree about whether something
  // is running.
  const firstPage = runsQuery.data?.pages[0];
  const hasActiveRun = firstPage !== undefined && anyRunActive(firstPage);

  // Whether this website has ever been crawled at all — `null` only while that is still
  // loading. The Trends tab needs it because the stats endpoint is entirely window-scoped and
  // cannot distinguish "nothing in the last 7 days" from "never run" (see `TrendsTab`).
  //
  // Derived from the run list this page already fetches, rather than a query of its own. Note
  // that a *failed* run query yields `false` here, not `null`, which is the same call
  // `headerStatus` above already makes with the same uncertainty — one page should not hold
  // two opinions about whether this website has ever run, and the Runs tab reports the
  // failure itself.
  const hasEverRun = runsQuery.isPending ? null : runs.length > 0;

  const isOwner = user !== null && website !== undefined && user.id === website.user_id;

  return (
    // `min-w-0` here and on every tab panel below is what actually keeps the page body from
    // scrolling sideways: without it a flex/grid child refuses to shrink below its content's
    // intrinsic width, and one long line in an llms.txt would widen the whole page instead
    // of scrolling inside the viewer that owns it.
    <div className="mx-auto w-full min-w-0 max-w-6xl px-4 py-8 sm:px-6">
      <Link
        href="/crawls"
        className="inline-flex items-center gap-1.5 rounded-md text-sm text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        All crawls
      </Link>

      {websiteQuery.isError ? (
        <div className="mt-6">
          <CrawlsError
            error={websiteQuery.error}
            onRetry={() => void websiteQuery.refetch()}
            isRetrying={websiteQuery.isFetching}
            hasStaleData={false}
          />
        </div>
      ) : (
        <>
          <header className="mt-6 flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              {website === undefined ? (
                <div className="space-y-2" aria-hidden="true">
                  <Skeleton className="h-7 w-64" />
                  <Skeleton className="h-4 w-40" />
                </div>
              ) : (
                <>
                  {/* The origin in Geist Mono. It is a URL, and the mono face is what stops
                      `rn` reading as `m` in a hostname somebody is checking. */}
                  <h1 className="truncate font-mono text-xl font-medium tracking-tight text-foreground">
                    {website.origin}
                  </h1>
                  {website.title !== null && (
                    <p className="mt-1 truncate text-sm text-muted-foreground">
                      {website.title}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                    <RunStatusIndicator status={headerStatus} />
                    <span aria-hidden="true" className="text-muted-foreground/40">
                      ·
                    </span>
                    <CrawlOwner userId={website.user_id} focusable />
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
                error={runsQuery.error}
                onRetry={() => void runsQuery.refetch()}
                isRetrying={runsQuery.isFetching}
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
                runsError={runsQuery.error}
                onRetryRuns={() => void runsQuery.refetch()}
                isRetryingRuns={runsQuery.isFetching}
                selectedRunId={selectedRunId}
                onSelectRun={showRunOutput}
                canRun={isOwner}
              />
            </TabsContent>

            <TabsContent value="schedule" className="mt-6 min-w-0">
              <ScheduleTab
                websiteId={websiteId}
                ownerUserId={website?.user_id ?? null}
                isOwner={isOwner}
                // The newest run, for the panel's "Last run" line. `ScheduleResponse` carries
                // a `last_run_at` but no status to go beside it, so the status comes from the
                // history this page has already fetched — which also keeps the panel and the
                // header from ever disagreeing about how the last run went.
                latestRun={runs[0] ?? null}
              />
            </TabsContent>

            <TabsContent value="trends" className="mt-6 min-w-0">
              <TrendsTab
                websiteId={websiteId}
                window={statsWindow}
                onWindowChange={setStatsWindow}
                // Radix already unmounts an inactive panel, so this is belt-and-braces — but
                // it is what actually holds the "no aggregate query on a page view that never
                // opens Trends" guarantee, rather than leaving it to a mounting detail.
                isActive={tab === "trends"}
                hasEverRun={hasEverRun}
                canRun={isOwner}
              />
            </TabsContent>
          </Tabs>
        </>
      )}
    </div>
  );
}
