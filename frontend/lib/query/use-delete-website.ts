"use client";

import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";
import { toast } from "sonner";

import { deleteWebsite } from "@/lib/api/websites";

import { queryKeys } from "./query-keys";

/**
 * `DELETE /websites/{id}`. On success this both invalidates `queryKeys.websites.all` (so
 * every mounted list drops the row on its next render) and removes the deleted website's
 * own detail entry from the cache outright, with `queryClient.removeQueries` rather than
 * `invalidateQueries` — an invalidated entry is refetched the next time something reads
 * it, and refetching `GET /websites/{id}` for a website that no longer exists would just
 * turn a stale-cache problem into a `404` a caller then has to handle. Removing it means
 * there is nothing left to refetch.
 *
 * Failure surfaces as a `sonner` toast from `error.message`, same as
 * `use-create-website.ts`. `options` composes the same way: a caller's own
 * `onSuccess`/`onError` runs after this hook's, never instead of it.
 */
export function useDeleteWebsite(
  options?: Pick<UseMutationOptions<void, Error, string>, "onSuccess" | "onError">
) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => deleteWebsite(id),
    onSuccess: (data, id, onMutateResult, context) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      queryClient.removeQueries({ queryKey: queryKeys.websites.detail(id) });
      options?.onSuccess?.(data, id, onMutateResult, context);
    },
    onError: (error, id, onMutateResult, context) => {
      toast.error(error.message);
      options?.onError?.(error, id, onMutateResult, context);
    },
  });
}
