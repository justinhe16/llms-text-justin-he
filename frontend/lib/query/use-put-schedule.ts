"use client";

import { useRef } from "react";
import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";
import { toast } from "sonner";

import { putSchedule, type Schedule, type UpsertScheduleRequest } from "@/lib/api/schedules";

import { queryKeys } from "./query-keys";

/**
 * The one call this mutation accepts: which website's schedule, and the two knobs
 * `UpsertScheduleRequest` allows a caller to set on it. Bundled into a single object rather
 * than curried into the hook itself (`usePutSchedule(websiteId)`) so this hook's shape
 * matches `useCreateWebsite`/`useDeleteWebsite` (lib/query/use-create-website.ts,
 * use-delete-website.ts): one hook call, no arguments, and every piece of per-call data —
 * here, two pieces instead of one — travels through `mutate()`'s single variables argument.
 */
export type PutScheduleInput = {
  websiteId: string;
  body: UpsertScheduleRequest;
};

/**
 * `PUT /websites/{id}/schedule`. On success this invalidates two keys, not one:
 *
 * - `queryKeys.schedules.detail(websiteId)` — the schedule itself; the obvious half.
 * - `queryKeys.websites.all` — every mounted websites list, including every `?include`
 *   variant. This is the half worth explaining: `GET /websites?include=latest_run` folds a
 *   `ScheduleSummary` into each row (`WebsiteListItemResponse.schedule`, see
 *   lib/api/websites.ts), so a list a component is already rendering can be showing this
 *   website's *previous* `active`/`interval_minutes` until it refetches. Skipping this
 *   invalidation would leave that fold stale even though the schedule itself, read on its
 *   own, is correct.
 *
 * `queryKeys.runs.*` is deliberately **not** invalidated. Nothing about `PUT
 * /websites/{id}/schedule` changes any run's own data — it only changes when the *next* run
 * might be enqueued, and that fact already lives on the schedule (`next_run_at`) and its
 * folded summary, both covered by the two invalidations above. A schedule edit has no
 * bearing on a run history a caller might already be looking at.
 *
 * `options` composes the same way `use-create-website.ts` and `use-delete-website.ts` do: a
 * caller's own `onSuccess`/`onError` runs after this hook's, never instead of it. Failure
 * surfaces as a `sonner` toast built from `error.message` — `ApiError`'s own wording (see
 * lib/api/fetcher.ts's `extractErrorMessage`), so a `403` from a non-owner reads as the
 * actual reason rather than "Request failed with status 403".
 */
/** Identifies one dispatch of this mutation, so a response can tell whether a newer write
 * has overtaken it. */
type PutScheduleContext = { requestId: number };

export function usePutSchedule(
  options?: Pick<
    UseMutationOptions<Schedule, Error, PutScheduleInput, PutScheduleContext>,
    "onSuccess" | "onError"
  >
) {
  const queryClient = useQueryClient();

  // Two writes to this endpoint really can overlap — the debounce in
  // lib/crawls/use-schedule-editor.ts is a timer, not a lock, so a change made while a
  // request is on the wire starts a second one. Counting dispatches is what lets the older
  // response recognise that it is no longer the truth.
  const dispatchCount = useRef(0);

  return useMutation({
    mutationFn: (input: PutScheduleInput) => putSchedule(input.websiteId, input.body),
    onMutate: (): PutScheduleContext => {
      dispatchCount.current += 1;
      return { requestId: dispatchCount.current };
    },
    onSuccess: (data, variables, onMutateResult, context) => {
      // The response *is* the new schedule — `PUT` echoes the row it just wrote, including
      // the `next_run_at` the server recomputed. Seeding the cache with it before
      // invalidating closes a window that is short but very visible: `invalidateQueries`
      // only *marks* the key stale and starts a refetch, so for the round-trip that follows,
      // any component reading this key still gets the pre-write value. On a panel whose
      // controls have already moved to the new setting (lib/crawls/use-schedule-editor.ts),
      // that reads as the toggle snapping back and then forward again.
      //
      // The invalidation below is kept rather than replaced. `setQueryData` updates this one
      // key from one response; the refetch is what reconciles anything the server changed
      // that the response does not carry, and keeps the "server is the source of truth"
      // property this codebase relies on everywhere else.
      //
      // Guarded by `requestId`, because responses can arrive out of order: if the older of
      // two overlapping writes resolves last, writing its body here would regress the cache
      // to a superseded value and flicker the panel back to it until the refetch corrects
      // it. Only the newest dispatch is allowed to seed the cache; an overtaken response
      // still invalidates, which is exactly the right amount of work for it to do.
      if (onMutateResult.requestId === dispatchCount.current) {
        queryClient.setQueryData(queryKeys.schedules.detail(variables.websiteId), data);
      }
      queryClient.invalidateQueries({
        queryKey: queryKeys.schedules.detail(variables.websiteId),
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      options?.onSuccess?.(data, variables, onMutateResult, context);
    },
    onError: (error, variables, onMutateResult, context) => {
      toast.error(error.message);
      options?.onError?.(error, variables, onMutateResult, context);
    },
  });
}
