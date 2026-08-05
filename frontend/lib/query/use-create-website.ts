"use client";

import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";
import { toast } from "sonner";

import { createWebsite, type Website } from "@/lib/api/websites";

import { queryKeys } from "./query-keys";

/**
 * `POST /websites`. Every call invalidates `queryKeys.websites.all` on success — every
 * website list currently mounted (with or without `?include=latest_run`) refetches to
 * pick up the new row — and surfaces a failure as a `sonner` toast built from
 * `error.message`, which is where `lib/api/fetcher.ts`'s `ApiError` puts the backend's own
 * wording (a `409` reads "you already have this website" instead of "Request failed with
 * status 409" — see that file's `extractErrorMessage`).
 *
 * `options` lets a caller add its own `onSuccess`/`onError` — a page that wants to
 * navigate on success, say, or the `409` handling `lib/api/errors.ts`'s
 * `isWebsiteAlreadyExists` exists for — **without** losing either behavior above: both
 * run, this hook's always first, so the invalidation and the toast have already happened
 * by the time a caller's own callback sees the result.
 */
export function useCreateWebsite(
  options?: Pick<UseMutationOptions<Website, Error, string>, "onSuccess" | "onError">
) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (url: string) => createWebsite(url),
    onSuccess: (data, variables, onMutateResult, context) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      options?.onSuccess?.(data, variables, onMutateResult, context);
    },
    onError: (error, variables, onMutateResult, context) => {
      toast.error(error.message);
      options?.onError?.(error, variables, onMutateResult, context);
    },
  });
}
