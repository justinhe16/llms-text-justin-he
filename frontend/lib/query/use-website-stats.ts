"use client";

import { useQuery } from "@tanstack/react-query";

import { getStats, type StatsWindow } from "@/lib/api/runs";

import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}/stats?window=`, for the detail page's Trends tab.
 *
 * ## `enabled`, and why it is a prop rather than an assumption
 *
 * Radix only renders the active tab's content, so a `TrendsTab` mounted inside
 * `<TabsContent value="trends">` does not exist while another tab is open, and this hook
 * would not run either way. `enabled` is still passed explicitly, from the caller's own
 * `tab === "trends"`, because the acceptance criterion is about the *request* ("the stats
 * query does not fire while another tab is active"), not about Radix's current mounting
 * behaviour. `forceMount` on a `TabsContent`, or a future switch to keeping panels alive to
 * preserve their scroll position, would quietly turn every detail-page load into an
 * aggregate query over 14 days of runs — and nothing would fail, it would just get slower.
 * One boolean makes that non-negotiable instead of incidental.
 *
 * ## No polling, and no invalidation on trigger either
 *
 * `pollWhileActive` (lib/query/polling.ts) has nothing to key off here: a stats response
 * carries counts and averages, not a status, so there is no "still in progress" for a
 * predicate to read. That is the same reason `useWebsite` and `useSchedule` do not poll, and
 * inventing an interval anyway would look like a feature and do nothing.
 *
 * `useTriggerRun` deliberately does *not* invalidate this key, either. It is tempting — a
 * queued run does bump `total_runs` by one — but a trend over 1 to 14 days does not
 * meaningfully change when a single `pending` run appears, and the refetch it would cost is
 * a full re-aggregation over the window. Switching windows refetches, and the 10s
 * `staleTime` in app/providers.tsx means simply leaving and re-entering the tab does too.
 * That is the freshness this screen actually needs.
 */
export function useWebsiteStats(
  websiteId: string,
  window: StatsWindow,
  options: { enabled: boolean }
) {
  return useQuery({
    queryKey: queryKeys.stats.detail(websiteId, window),
    queryFn: () => getStats(websiteId, window),
    enabled: options.enabled,

    // Keep the previous window's response on screen while the next one loads, instead of
    // dropping back to the skeleton. Switching 7d → 14d is a refinement of what is already
    // being read, and blanking four tiles and three charts for the length of a round trip
    // makes the switcher feel like it reloaded the page. `isFetching` is what the panel
    // dims on meanwhile, so the stale data never claims to be current.
    placeholderData: (previous) => previous,
  });
}
