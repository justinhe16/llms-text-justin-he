"use client";

import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";
import { toast } from "sonner";

import { updateWebsite, type Website } from "@/lib/api/websites";

import { queryKeys } from "./query-keys";

/** The one call this mutation accepts: which website, and its new `enrich_with_llm` value. */
export type UpdateWebsiteInput = {
  websiteId: string;
  enrichWithLlm: boolean;
};

/**
 * `PATCH /websites/{id}`. Mirrors `usePutSchedule` (lib/query/use-put-schedule.ts) —
 * `setQueryData` first, then `invalidateQueries`, for the identical reason that hook's own
 * docstring gives: `invalidateQueries` only marks the key stale and starts a refetch, so
 * seeding the cache with the response closes the window where `EnrichmentPanel`'s control has
 * already moved to the new value but the round trip has not landed, which would otherwise
 * read as the toggle snapping back and forward again.
 *
 * **No `requestId` out-of-order guard**, unlike `usePutSchedule`. That guard exists because
 * `use-schedule-editor.ts` debounces two controls sharing one request body, so two writes can
 * genuinely overlap in flight. This hook has no debounce (see `EnrichmentPanel`'s own
 * docstring for why a single boolean does not need one), so at most one request from this
 * hook is ever in flight from one panel at a time, and the guard would be dead code.
 *
 * `queryKeys.runs.*` is deliberately **not** invalidated — changing this toggle affects the
 * next run only, never a run already recorded, and invalidating the run history here would be
 * the machine-checkable half of the `[UI — timing]` acceptance criterion failing quietly.
 *
 * Failure surfaces as a `sonner` toast built from `error.message` — `ApiError`'s own wording
 * (lib/api/fetcher.ts's `extractErrorMessage`), so a `403` from a non-owner reads as the
 * backend's actual reason.
 */
export function useUpdateWebsite(
  options?: Pick<
    UseMutationOptions<Website, Error, UpdateWebsiteInput>,
    "onSuccess" | "onError"
  >
) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (input: UpdateWebsiteInput) =>
      updateWebsite(input.websiteId, input.enrichWithLlm),
    onSuccess: (data, variables, onMutateResult, context) => {
      queryClient.setQueryData(queryKeys.websites.detail(variables.websiteId), data);
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      options?.onSuccess?.(data, variables, onMutateResult, context);
    },
    onError: (error, variables, onMutateResult, context) => {
      toast.error(error.message);
      options?.onError?.(error, variables, onMutateResult, context);
    },
  });
}
