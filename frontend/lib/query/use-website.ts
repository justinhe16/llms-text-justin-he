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
 * (lib/query/polling.ts) to evaluate. Inventing a poll here would mean refetching on a
 * timer for a reason this data can't express, which is worse than not polling: it looks
 * like a feature and does nothing. This hook starts polling the day `GET /websites/{id}`
 * itself gains run information, or the day a `runs` endpoint exists to poll instead
 * (lib/query/query-keys.ts's note on where that key slots in).
 */
export function useWebsite(id: string) {
  return useQuery({
    queryKey: queryKeys.websites.detail(id),
    queryFn: () => getWebsite(id),
  });
}
