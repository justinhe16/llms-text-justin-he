"use client";

import { useQuery } from "@tanstack/react-query";

import { anyRunActive } from "@/lib/api/run-status";
import { listRuns, type RunListOptions } from "@/lib/api/runs";

import { pollWhileActive } from "./polling";
import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}/runs`, kept fresh while any run on the fetched page is still in
 * progress — the "Runs tab" case this hook exists for: a website's run history, rendered as
 * a list, where any row could still be `pending`/`processing`.
 *
 * Polls on `anyRunActive` (lib/api/run-status.ts), which is `isActiveRunStatus` folded over
 * `RunPage["items"]` — the same underlying predicate `useWebsites` polls a website list on
 * and `useRun` below polls a single run on. One definition of "still running" serves three
 * response shapes; only the fold changes per call site.
 */
export function useRuns(websiteId: string, options?: RunListOptions) {
  return useQuery({
    queryKey: queryKeys.runs.list(websiteId, options),
    queryFn: () => listRuns(websiteId, options),
    refetchInterval: pollWhileActive(anyRunActive),
  });
}
