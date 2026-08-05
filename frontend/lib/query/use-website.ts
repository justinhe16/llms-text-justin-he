"use client";

import { useQuery } from "@tanstack/react-query";

import { getWebsite } from "@/lib/api/websites";

import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}`. Deliberately does **not** poll.
 *
 * `WebsiteResponse` (backend/app/features/websites/schemas.py) carries no run or schedule
 * information at all — that fold only exists on `GET /websites` via `?include=latest_run`
 * (`WebsiteListItemResponse`) — so there is nothing on this response for `pollWhileActive`
 * (lib/query/polling.ts) to evaluate. That is still true now that the runs feature has
 * landed: a website's detail screen gets its run data, and therefore its polling, by
 * composing this hook with `useRuns(id)` (the run history) and/or `useRun(runId)` (a single
 * run's own detail) — both in this directory, both built on the same `pollWhileActive` this
 * hook does not use. Inventing a poll here instead of on those hooks would mean refetching
 * on a timer for a reason this response can't express, which is worse than not polling: it
 * looks like a feature and does nothing.
 */
export function useWebsite(id: string) {
  return useQuery({
    queryKey: queryKeys.websites.detail(id),
    queryFn: () => getWebsite(id),
  });
}
