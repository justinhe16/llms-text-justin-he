"use client";

import { useCallback, useMemo, useState } from "react";

import { useStatusChangeHighlight } from "@/lib/crawls/use-status-change-highlight";
import { DEFAULT_SORT, nextSortState, sortWebsites, type SortKey } from "@/lib/crawls/sort";
import { useWebsites } from "@/lib/query/use-websites";

import { CrawlsCardList } from "./crawls-card-list";
import { CrawlsEmpty } from "./crawls-empty";
import { CrawlsError } from "./crawls-error";
import { CrawlsSkeleton } from "./crawls-skeleton";
import { CrawlsTable } from "./crawls-table";

/**
 * The /crawls content area: fetch, sort, and pick which of the four states to render.
 *
 * The data comes from `useWebsites` (lib/query/use-websites.ts) exactly as it already
 * exists — `?include=latest_run` for the folded run and schedule, and polling that starts
 * and stops on its own. This component adds no polling logic of its own and no second
 * definition of "still running": the interval, the predicate, and the pause-on-hidden-tab
 * behaviour all live where ARCHITECTURE.md §8.6 says they live. The only reason
 * `include: "latest_run"` matters here is that without it every `latest_run` comes back
 * `null`, and a table of five empty columns would poll forever on a predicate that can
 * never be true.
 */
export function CrawlsView() {
  const { data, error, isPending, isFetching, refetch } = useWebsites({
    include: "latest_run",
  });
  const [sort, setSort] = useState(DEFAULT_SORT);

  const highlighted = useStatusChangeHighlight(data);

  const sorted = useMemo(() => (data ? sortWebsites(data, sort) : []), [data, sort]);

  const handleSort = useCallback((key: SortKey) => {
    setSort((current) => nextSortState(current, key));
  }, []);

  const handleRetry = useCallback(() => {
    void refetch();
  }, [refetch]);

  // Order matters here, and it is the one thing in this component worth reading twice.
  //
  // `isPending` is React Query's "no data has ever arrived", so it is the only state that
  // gets the skeleton — a refetch behind existing data is `isFetching`, not `isPending`, and
  // must not replace the table with placeholders.
  if (isPending) return <CrawlsSkeleton />;

  // A failure with nothing cached: the error is the whole screen.
  if (error && data === undefined) {
    return (
      <CrawlsError error={error} onRetry={handleRetry} isRetrying={isFetching} hasStaleData={false} />
    );
  }

  // Past this point there is data. A failure now is a *refetch* that failed, and it goes
  // above the table without disturbing it.
  const staleError = error ? (
    <CrawlsError error={error} onRetry={handleRetry} isRetrying={isFetching} hasStaleData />
  ) : null;

  if (sorted.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        {staleError}
        <CrawlsEmpty />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {staleError}

      {/* Two markup trees behind Tailwind breakpoints, never a measured viewport width —
          see the comment in crawls-table.tsx. */}
      <div className="hidden overflow-hidden rounded-xl border border-border bg-card md:block">
        <CrawlsTable
          websites={sorted}
          sort={sort}
          onSort={handleSort}
          highlighted={highlighted}
        />
      </div>

      <div className="md:hidden">
        <CrawlsCardList websites={sorted} highlighted={highlighted} />
      </div>
    </div>
  );
}
