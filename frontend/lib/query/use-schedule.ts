"use client";

import { useQuery } from "@tanstack/react-query";

import { getSchedule } from "@/lib/api/schedules";

import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}/schedule`. Deliberately does **not** poll.
 *
 * `useWebsite` (lib/query/use-website.ts) skips polling because its response has nothing on
 * it for `pollWhileActive` (lib/query/polling.ts) to evaluate at all. A schedule is a
 * different case, worth stating on its own terms rather than by analogy: unlike a run, a
 * schedule has no in-progress state to wait out — there is no "processing" schedule, only
 * "configured" or "not." It changes for exactly one reason, a person submitting `PUT
 * /websites/{id}/schedule`, and `lib/query/use-put-schedule.ts` already invalidates this
 * exact query the moment that happens. Polling on top of that would mean refetching on a
 * timer to notice a change this app already learns about immediately, through cache
 * invalidation — the same "looks like a feature, does nothing" waste `useWebsite`'s own
 * comment warns against, just reached by a different route.
 */
export function useSchedule(websiteId: string) {
  return useQuery({
    queryKey: queryKeys.schedules.detail(websiteId),
    queryFn: () => getSchedule(websiteId),
  });
}
