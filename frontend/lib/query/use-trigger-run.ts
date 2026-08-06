"use client";

import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";
import { toast } from "sonner";

import { ApiError } from "@/lib/api/fetcher";
import { isRunAlreadyInFlight } from "@/lib/api/errors";
import { triggerRun, type TriggeredRun } from "@/lib/api/runs";

import { queryKeys } from "./query-keys";

/**
 * What the caller learns from a failed trigger, beyond the toast this hook has already
 * shown. Every field is derived from the response the backend actually sent — nothing here
 * is inferred from prose.
 */
export type TriggerRunFailure = {
  /**
   * The run that was already in flight, when the failure was a `409`. This is the whole
   * reason a `409` is worth distinguishing: the useful reaction is to show the user the run
   * that is already going, not to tell them they can't start one. `null` for every other
   * failure.
   */
  existingRunId: string | null;

  /**
   * `true` when the queue itself is unreachable (`503`) — the run row was written but
   * nothing will ever pick it up. Distinct from a generic failure because the advice is
   * different: this is not "you did something wrong", it is "the crawler is down, try
   * later", and it must not be quietly rendered as a pending run that will never move.
   */
  queueUnavailable: boolean;
};

/**
 * `POST /websites/{id}/runs` — the "Run now" button (components/crawls/run-now-button.tsx).
 *
 * ## What this hook does on its own
 *
 * On success it invalidates two things:
 *
 * - `queryKeys.runs.forWebsite(websiteId)` — every cached page and filter variant of *this*
 *   website's history, so the new `pending` row appears. Not `runs.all`, which would also
 *   drop every `runs.detail` entry including the `llms_txt` the Output tab may be
 *   displaying; see that key's own comment.
 * - `queryKeys.websites.all` — every mounted websites list. `GET /websites?include=latest_run`
 *   folds a `LatestRunSummary` into each row, and that fold is now stale for this website.
 *   The same reasoning `use-put-schedule.ts` gives for its second invalidation.
 *
 * Polling takes over from there: the refetched first page contains a `pending` run, which
 * makes `useRunsInfinite`'s predicate true, which starts the three-second tick until the
 * run reaches a terminal status.
 *
 * ## Errors
 *
 * Every failure gets a toast built from `error.message`, which is the backend's own
 * wording — `lib/api/fetcher.ts`'s `extractErrorMessage` lifts `detail.message` off the
 * structured bodies, so the `429` cap message ("Daily limit reached (N runs in 24 hours).
 * Resets in 3h 12m.") reaches the user verbatim, reset time included, without this hook
 * rebuilding the sentence from `scope` and `resets_at`. That is the ticket's requirement
 * met by the layer that already solved it, not by a special case here.
 *
 * What this hook adds on top is the branching a message cannot express, handed to the
 * caller as a `TriggerRunFailure`:
 *
 * - **`409`** — `existingRunId` is set, and the caller selects that run instead of
 *   dead-ending. The `403`-shaped mistake would be to treat this as an error the user has
 *   to clear; it is a redirect with a message attached.
 * - **`429`** — nothing to add. `scope`/`resets_at` are available through
 *   `lib/api/errors.ts`'s `isRunLimitExceeded` if a future caller wants to render them
 *   structurally, but the verbatim message is what this ticket asks for and it is already
 *   in the toast.
 * - **`503`** — `queueUnavailable`, so the caller can avoid optimistically showing a run
 *   that is never going to start.
 * - **`403`** — no special field. The button is gated on ownership, so reaching this means
 *   the gate was working from stale data (a second tab signed in as someone else, a
 *   `user_id` that changed under the page). The backend's message says exactly that, the
 *   toast shows it, and the important part is simply that the page does not crash — which
 *   is what the ticket asks for and what falling through to the default branch gives.
 *
 * `options` composes the way it does in `use-create-website.ts`, `use-delete-website.ts`,
 * and `use-put-schedule.ts`: a caller's own `onSuccess`/`onError` runs *after* this hook's,
 * never instead of it, so the invalidations and the toast have already happened by the time
 * the caller's callback sees the result.
 *
 * `toastOnError` defaults to `true` and exists for the same reason as its twin in
 * `use-create-website.ts`: the landing page's submit flow renders every failure inline,
 * under the URL field, and the `409` it handles is not an error the user has to clear at
 * all — it navigates. A toast there would either duplicate the inline message or announce a
 * failure at the exact moment the page is doing the right thing. Everything else this hook
 * does, including the `TriggerRunFailure` handed to `onFailure`, is unaffected.
 */
export function useTriggerRun(
  options?: Pick<UseMutationOptions<TriggeredRun, Error, string>, "onSuccess"> & {
    onFailure?: (failure: TriggerRunFailure, error: Error) => void;
    toastOnError?: boolean;
  }
) {
  const queryClient = useQueryClient();
  const toastOnError = options?.toastOnError ?? true;

  return useMutation({
    mutationFn: (websiteId: string) => triggerRun(websiteId),

    onSuccess: (data, websiteId, onMutateResult, context) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs.forWebsite(websiteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      options?.onSuccess?.(data, websiteId, onMutateResult, context);
    },

    onError: (error: Error) => {
      if (toastOnError) toast.error(error.message);

      const alreadyInFlight = isRunAlreadyInFlight(error);
      options?.onFailure?.(
        {
          existingRunId: alreadyInFlight?.run_id ?? null,
          queueUnavailable: error instanceof ApiError && error.status === 503,
        },
        error
      );
    },
  });
}
