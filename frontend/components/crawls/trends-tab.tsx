"use client";

import type { StatsWindow } from "@/lib/api/runs";
import { outcomeBreakdown, toTrendPoints, windowHasRuns } from "@/lib/crawls/stats-display";
import { useWebsiteStats } from "@/lib/query/use-website-stats";

import { CrawlsError } from "./crawls-error";
import { DurationChart, OutcomeChart, PagesChart, TrendsChartsSkeleton } from "./trends-charts";
import { TrendsStatTiles, TrendsStatTilesSkeleton } from "./trends-stat-tiles";
import { TrendsWindowSwitcher } from "./trends-window-switcher";

interface TrendsTabProps {
  websiteId: string;
  /** The selected window, from the URL. */
  window: StatsWindow;
  onWindowChange: (next: StatsWindow) => void;
  /**
   * Whether this tab is the visible one. Drives the query's `enabled` — see
   * `lib/query/use-website-stats.ts` for why that is passed in rather than assumed from
   * Radix's mounting behaviour.
   */
  isActive: boolean;
  /**
   * Whether this website has *ever* been crawled, or `null` while that is still unknown.
   *
   * This cannot be answered from the stats response. Every field on `totals` is scoped to the
   * window, `last_run_at` included, so a site crawled twice last year and a site never
   * crawled at all report byte-identical zeroes. The detail page's run history is unscoped
   * and does know, which is why this arrives as a prop instead of a second query — the page
   * already fetches that list once for three other consumers (see `website-detail.tsx`).
   */
  hasEverRun: boolean | null;
  /** Whether the reader may trigger a run, so the never-run state points at something they
   * can actually do rather than at a button that is disabled for them. */
  canRun: boolean;
}

/**
 * The Trends tab: four stat tiles and three charts over `GET /websites/{id}/stats`.
 *
 * ## Four states, and the one distinction that needs two queries
 *
 * "No runs in the last 30 days" and "this site has never been crawled" want completely
 * different words — the first is a fact about a window the reader can widen, the second is an
 * invitation to press Run now — and the stats endpoint cannot tell them apart, because
 * everything it returns is window-scoped. So the never-run answer comes from `hasEverRun`,
 * and while that is still `null` this panel keeps showing its skeleton rather than guessing.
 *
 * Guessing would not look like an error; it would look like a correct panel that says the
 * wrong thing for a moment. Deep-linking to `?tab=trends` starts both queries at once, and
 * the run list — one page of rows — lands well before a ninety-day aggregate does, so the
 * extra wait is theoretical in the case that actually occurs.
 *
 * `hasEverRun` is `false` rather than `null` when the run query *failed*, which deliberately
 * mirrors what the page header already does with the same uncertainty (`website-detail.tsx`
 * falls back to the `never-run` status when no runs loaded). One page should not hold two
 * opinions about whether a website has ever been crawled, and the failure itself is already
 * reported, loudly, by the Runs tab.
 */
export function TrendsTab({
  websiteId,
  window,
  onWindowChange,
  isActive,
  hasEverRun,
  canRun,
}: TrendsTabProps) {
  const statsQuery = useWebsiteStats(websiteId, window, { enabled: isActive });
  const stats = statsQuery.data;

  // Ordered so a failed refetch never blanks a panel that is already readable, matching
  // `RunsTab`: with data on screen the error is a strip above it, and only a failure with
  // nothing to show takes over. `placeholderData` in the hook keeps the previous window's
  // response through a failed switch, so `hasStaleData` is genuinely often true here.
  if (statsQuery.isError) {
    return (
      <div className="space-y-4">
        <CrawlsError
          error={statsQuery.error}
          onRetry={() => void statsQuery.refetch()}
          isRetrying={statsQuery.isFetching}
          hasStaleData={stats !== undefined}
        />
        {stats !== undefined && (
          <TrendsPanel
            stats={stats}
            window={window}
            onWindowChange={onWindowChange}
            isFetching={statsQuery.isFetching}
          />
        )}
      </div>
    );
  }

  // `stats === undefined` covers the first load; `hasEverRun === null` covers the window
  // above where the two empty states are still indistinguishable.
  if (stats === undefined || hasEverRun === null) {
    return <TrendsSkeleton window={window} onWindowChange={onWindowChange} />;
  }

  if (!hasEverRun) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-border bg-card px-6 py-16 text-center">
        <p className="text-sm font-medium text-foreground">Run a crawl to see trends</p>
        <p className="max-w-sm text-sm text-muted-foreground">
          {canRun
            ? "Use Run now above to crawl this site. Once it has a run or two, its page counts and durations show up here."
            : "Nothing has crawled this site yet. Its owner can start a run from this page."}
        </p>
      </div>
    );
  }

  return (
    <TrendsPanel
      stats={stats}
      window={window}
      onWindowChange={onWindowChange}
      isFetching={statsQuery.isFetching}
    />
  );
}

/**
 * The loaded panel. Split out so the error branch above can render it underneath a stale-data
 * strip without duplicating the layout.
 */
function TrendsPanel({
  stats,
  window,
  onWindowChange,
  isFetching,
}: {
  stats: NonNullable<ReturnType<typeof useWebsiteStats>["data"]>;
  window: StatsWindow;
  onWindowChange: (next: StatsWindow) => void;
  isFetching: boolean;
}) {
  const points = toTrendPoints(stats);
  const breakdown = outcomeBreakdown(stats.totals);

  // One question asked once and handed to all three charts, so they cannot disagree about
  // whether this window is empty.
  const isEmptyWindow = !windowHasRuns(stats.totals);

  return (
    <div className="min-w-0 space-y-4">
      <TrendsHeader window={window} onWindowChange={onWindowChange} />

      {/* Dimmed, not replaced, while the next window loads. `placeholderData` keeps the
          previous response on screen through the switch (see use-website-stats.ts); this is
          what stops that stale data from claiming to be current. `aria-busy` says the same
          thing to a screen reader without moving focus or announcing the whole panel again. */}
      <div
        aria-busy={isFetching || undefined}
        className={
          isFetching
            ? "space-y-4 opacity-60 transition-opacity"
            : "space-y-4 transition-opacity"
        }
      >
        <TrendsStatTiles totals={stats.totals} />

        <div className="space-y-4">
          {/* `stats.bucket` — the API's own field — decides the axis labels. Never derived
              from `window` here or anywhere below; see `formatBucketTick`. */}
          <PagesChart points={points} bucket={stats.bucket} isEmpty={isEmptyWindow} />
          <DurationChart points={points} bucket={stats.bucket} isEmpty={isEmptyWindow} />
          <OutcomeChart breakdown={breakdown} isEmpty={isEmptyWindow} />
        </div>
      </div>
    </div>
  );
}

/** The switcher and the timezone note — the one row that is identical in the loading and
 * loaded states, so the controls do not appear late or move when data lands. */
function TrendsHeader({
  window,
  onWindowChange,
  disabled = false,
}: {
  window: StatsWindow;
  onWindowChange: (next: StatsWindow) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <TrendsWindowSwitcher value={window} onChange={onWindowChange} disabled={disabled} />
      {/* Buckets are UTC days and UTC hours (the backend truncates on them), and the axis
          labels say so rather than being converted into the reader's own zone, which would
          misattribute a run to the neighbouring day for most of the world. See the header
          comment in lib/crawls/stats-display.ts. */}
      <p className="text-xs text-muted-foreground">Times in UTC</p>
    </div>
  );
}

/** The loading state: the real switcher over placeholder tiles and chart-shaped boxes, at the
 * sizes the real ones occupy, so nothing moves when the data arrives. */
function TrendsSkeleton({
  window,
  onWindowChange,
}: {
  window: StatsWindow;
  onWindowChange: (next: StatsWindow) => void;
}) {
  return (
    <div className="min-w-0 space-y-4">
      {/* The switcher stays live rather than being drawn as a grey box: it is the one control
          on this panel, its state comes from the URL rather than from the response, and
          disabling it during the very wait someone would want to shorten is the wrong
          instinct. */}
      <TrendsHeader window={window} onWindowChange={onWindowChange} />
      <TrendsStatTilesSkeleton />
      <TrendsChartsSkeleton />
      <span role="status" aria-live="polite" className="sr-only">
        Loading trends
      </span>
    </div>
  );
}
