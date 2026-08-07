"use client";

import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";

import { getLlmsFullTxt } from "@/lib/api/runs";

import { llmsFullTxtFilename } from "./run-display";
import { saveTextFile } from "./save-text-file";

/**
 * Which artifact one click asks for: the run to fetch, and the origin its saved file is named
 * after. Carried together as one value so the two can never be sourced from different renders
 * — see the comment on `mutationFn` below.
 */
type DownloadTarget = { runId: string; origin: string };

/**
 * Fetches `GET /runs/{id}/llms-full.txt` on demand and saves it — the behaviour behind the
 * Output tab's "Download full" button (`components/crawls/download-full-button.tsx`).
 *
 * ## Why this lives in `lib/crawls/`, not `lib/query/`
 *
 * `lib/query/` is where a fetch earns a place when it needs the machinery that module owns:
 * a cache key (`query-keys.ts`), an invalidation on some other mutation's success, or
 * `pollWhileActive` (ARCHITECTURE.md §8.6). A download needs none of that. Its result is
 * never read back from a cache — the artifact goes straight to `saveTextFile` and then to the
 * browser's own download UI, never onto the screen — so there is no `queryKeys.*` entry to
 * define and nothing for a future mutation to invalidate. What is left is a `useMutation` with
 * a `mutationFn` and two callbacks, which is exactly the "non-visual logic that belongs beside
 * the feature, not beside the shared plumbing" `lib/<feature>/` is for (ARCHITECTURE.md §8.4,
 * the same rule `lib/landing/use-add-site.ts` follows for its own orchestration). A future
 * reader who sees `useMutation` and reaches for `lib/query/` by reflex should read this
 * paragraph first: it stays here on purpose.
 *
 * ## Why `useMutation` and not `useQuery`
 *
 * A download is triggered by a click, runs once, and produces a side effect (a file saved to
 * disk) rather than data this component or any other renders — there is nothing here for
 * React Query to cache, refetch, or dedupe, which is the same shape `useTriggerRun`
 * (lib/query/use-trigger-run.ts) already argued for its own one-shot `POST`.
 *
 * ## Errors
 *
 * The backend's two `404`s for this route (`RunService.get_llms_full_txt`) both carry a plain
 * string `detail` — "no such run" and "that run has not produced this artifact" read
 * identically to a client, and `lib/api/fetcher.ts`'s `extractErrorMessage` already lifts
 * either one onto `ApiError.message` with no further unwrapping needed, so `error.message` is
 * shown verbatim rather than routed through a new guard in `lib/api/errors.ts` — that module
 * is for structured `detail`s a caller branches on, and this route's `404`s are both just
 * prose to display.
 */
export function useDownloadFullArtifact(runId: string, origin: string) {
  const mutation = useMutation({
    // Which run, and which origin to name the file after, travel as the mutation's
    // *variables* rather than being read out of the enclosing render's closure — the shape
    // `useTriggerRun` (lib/query/use-trigger-run.ts) already uses for its own `websiteId`.
    // React Query patches a still-pending mutation's options on every re-render, so a
    // closure-captured `runId` is in principle a different value by the time the request is
    // dispatched. The stronger reason is the pairing: `onSuccess` names the file from the
    // SAME `origin` that travelled with the request that produced the bytes, so the artifact
    // and its filename cannot describe two different websites even in principle. A closure
    // would leave that resting on an argument about timing instead of on the code's shape.
    mutationFn: (target: DownloadTarget) => getLlmsFullTxt(target.runId),
    onSuccess: (text, target) => {
      const filename = llmsFullTxtFilename(target.origin);
      saveTextFile(text, filename);
      toast.success(`Downloaded ${filename}`);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return {
    download: () => mutation.mutate({ runId, origin }),
    isDownloading: mutation.isPending,
  };
}
