"use client";

import { useQuery } from "@tanstack/react-query";

import { anyWebsiteHasActiveRun } from "@/lib/api/run-status";
import { listWebsites } from "@/lib/api/websites";

import { pollWhileActive } from "./polling";
import { queryKeys } from "./query-keys";

/**
 * `GET /websites`, kept fresh while any returned website has a run in progress.
 *
 * Polling here only means anything when `options.include` is `"latest_run"` —
 * `anyWebsiteHasActiveRun` (lib/api/run-status.ts) reads `latest_run.status` off each row,
 * and that field is `null` on every row unless this fold was requested (see
 * `WebsiteListItemResponse`'s own docstring in backend/app/features/websites/schemas.py).
 * Calling this hook without `include: "latest_run"` still works — it just never has
 * anything to poll on, since `anyWebsiteHasActiveRun` sees `null` everywhere and returns
 * `false` every time.
 */
export function useWebsites(options?: { include?: "latest_run" }) {
  return useQuery({
    queryKey: queryKeys.websites.list(options?.include),
    queryFn: () => listWebsites(options),
    refetchInterval: pollWhileActive(anyWebsiteHasActiveRun),
  });
}
