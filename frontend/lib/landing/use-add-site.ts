"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { isRunAlreadyInFlight, isWebsiteAlreadyExists } from "@/lib/api/errors";
import { ApiError } from "@/lib/api/fetcher";
import { useCreateWebsite } from "@/lib/query/use-create-website";
import { useTriggerRun } from "@/lib/query/use-trigger-run";

import { parseSiteUrl } from "./site-url";

/**
 * What the landing page renders under its URL field when the sequence below does not end in
 * a navigation. Not a bare string, because two of the three failures are still actionable
 * and a sentence with nothing to click is the dead end this whole flow exists to avoid.
 */
export type AddSiteError = {
  /**
   * What to show. For anything the backend rejected this is `ApiError.message`, which
   * `lib/api/fetcher.ts` already lifted from the response's own `detail` — so the `429`'s
   * cap text reaches the user verbatim, limit and reset time included, rather than being
   * rebuilt here from `scope` and `resets_at`.
   */
  message: string;

  /**
   * Set once `POST /websites` has succeeded — i.e. every failure from the *second* request.
   * The site exists at this point even though its first run does not, so the UI can offer a
   * link to it instead of leaving the user with a URL they can no longer add.
   */
  websiteId: string | null;

  /** Whether trying the same thing again could plausibly work: a network blip, a `5xx`, the
   *  `503` from an unreachable queue. False for a cap (`429`) or a rejected URL, where the
   *  honest answer is that retrying now changes nothing. */
  retryable: boolean;
};

/**
 * How long to wait for `router.push` to actually take this page away before concluding it
 * never will (see the watchdog in `useAddSite`). Generous on purpose: the destination is a
 * dynamic route that fetches on navigate, and firing this while a slow-but-working
 * navigation is still in flight would replace a correct spinner with a wrong error.
 */
const NAVIGATION_WATCHDOG_MS = 15_000;

/** Turns a caught error into the two fields above that are not the id. */
function describeFailure(error: unknown): { message: string; retryable: boolean } {
  if (error instanceof ApiError) {
    // 5xx — including the queue's 503 — is the server's problem, not the input's, and is
    // the one class of failure where pressing the button again can genuinely work. 4xx
    // (401, 403, 422, and the 429 cap) is not fixed by retrying, so offering a retry there
    // would be a lie. There is no separate 503 branch: it wants exactly this treatment.
    return { message: error.message, retryable: error.status >= 500 };
  }
  // `apiFetch` only raises `ApiError` for a non-2xx *response*; a genuine network failure
  // (offline, DNS, connection reset) propagates as fetch's own `TypeError`, whose message
  // ("Failed to fetch") is not something to show a person.
  return {
    message: "Couldn't reach the server. Check your connection and try again.",
    retryable: true,
  };
}

/**
 * The landing page's submit flow: validate, create the website, start its first run, and
 * only then navigate to the detail page.
 *
 * ## Why both requests complete before the navigation
 *
 * Navigating optimistically after the `201` would land on `/crawls/{id}` with an empty Runs
 * tab and a run that appears a beat later, which reads as a broken page rather than a fast
 * one. The wait is real, which is why the field renders a spinner and stays disabled across
 * both requests.
 *
 * ## The five endings
 *
 * | outcome | what happens |
 * | --- | --- |
 * | `201` then `202` | navigate to `/crawls/{id}` — the ordinary path |
 * | `409` from `POST /websites` | navigate to the website already in the body. Pasting a URL you already added should take you to it, not error. |
 * | `409` from `POST /websites/{id}/runs` | navigate anyway — the run already in flight is the one they want to watch |
 * | `429` | stay, and render the backend's cap message inline, verbatim |
 * | `503` / `5xx` / network | stay, render the message, offer a retry — and, when the site was already created, a link to it |
 *
 * The `503` case is the one worth stating: the queue could not be reached, and
 * `RunService.abandon_unqueued_run` has already marked the row `failed` before answering. So
 * there is no run in flight to go and watch — sending the user to a detail page to look at a
 * run that never started would be worse than saying so here and offering a retry that can
 * actually work.
 *
 * Both mutations run with `toastOnError: false`. Every message here belongs under the field
 * that produced it — and on the two `409` paths there is no failure to report at all, only a
 * navigation.
 */
export function useAddSite(): {
  /** Validate `input` and run the sequence. Safe to call on Enter and on the arrow button. */
  submit: (input: string) => void;
  /** Re-run whatever failed — just the trigger if the website already exists, the whole
   *  sequence otherwise. No-op unless there is a retryable error showing. */
  retry: () => void;
  /** Clear the inline message, e.g. as soon as the user edits the field. */
  clearError: () => void;
  error: AddSiteError | null;
  /** True from the first request until the navigation commits: the field is disabled and
   *  showing a spinner for exactly this long. */
  isBusy: boolean;
} {
  const router = useRouter();
  const [error, setError] = useState<AddSiteError | null>(null);
  // Stays true through the `router.push` below. Letting it fall back to false the instant
  // the request resolves would re-enable the field for the frame or two before the new
  // route paints, which looks like the submit did nothing.
  const [isNavigating, setIsNavigating] = useState(false);
  // What was last submitted, so `retry()` can resend it without the field having to hand it
  // back — the field may have been cleared or edited in between.
  const lastUrl = useRef<string | null>(null);

  // The website the pending `router.push` is heading for, so the watchdog below can still
  // point at it if that push never lands.
  const pendingWebsiteId = useRef<string | null>(null);

  // The watchdog on `isNavigating`.
  //
  // That flag is a one-way latch: it disables the field until the new route takes over, and
  // what normally clears it is this component unmounting. Nothing guarantees that unmount.
  // `middleware.ts` can bounce `/crawls/{id}` straight back to `/` (a server session that
  // lapsed between the POST and the push), the router can restore this page from its cache,
  // or the push can simply not land — and in every one of those cases the field would sit
  // frozen behind a spinner with no error and no way out but a reload.
  //
  // A route-change check does not catch that: the redirect case starts and ends on `/`, so
  // `usePathname()` never changes and an effect keyed on it never re-runs. Elapsed time is
  // the only signal that covers all of them, because the thing being detected is precisely
  // "the navigation we expected did not happen". If the push works, this component is gone
  // long before the timer fires and the cleanup cancels it.
  useEffect(() => {
    if (!isNavigating) return;
    const timer = setTimeout(() => {
      setIsNavigating(false);
      setError({
        // Deliberately does not say "the site was added": this also fires on the
        // already-exists path, where the site was not added by this submit.
        message: "That took longer than expected, and the page didn't move.",
        websiteId: pendingWebsiteId.current,
        retryable: false,
      });
    }, NAVIGATION_WATCHDOG_MS);
    return () => clearTimeout(timer);
  }, [isNavigating]);

  const createWebsiteMutation = useCreateWebsite({ toastOnError: false });
  const triggerRunMutation = useTriggerRun({ toastOnError: false });

  const { mutateAsync: createWebsite } = createWebsiteMutation;
  const { mutateAsync: triggerRun } = triggerRunMutation;

  const navigate = useCallback(
    (websiteId: string) => {
      pendingWebsiteId.current = websiteId;
      setIsNavigating(true);
      router.push(`/crawls/${websiteId}`);
    },
    [router]
  );

  const startRun = useCallback(
    async (websiteId: string): Promise<void> => {
      let triggered: boolean;
      try {
        await triggerRun(websiteId);
        triggered = true;
      } catch (caught) {
        // `409` — this website already has a run going. That run is what the user came to
        // see, so this is a redirect with a message attached, not a failure.
        if (isRunAlreadyInFlight(caught)) {
          triggered = true;
        } else {
          // Everything else stays on this page. `503` is the one worth naming: the queue
          // could not be reached, and `RunService.abandon_unqueued_run` has already marked
          // the row `failed` before answering — so there is no run in flight to go and
          // watch, and sending the user to a detail page to look at a run that never
          // started would be worse than saying so here and offering the retry that can
          // actually work.
          const { message, retryable } = describeFailure(caught);
          setError({ message, websiteId, retryable });
          triggered = false;
        }
      }

      // Outside the `try`, so a throw from `router.push` is never caught above and
      // mis-reported as "Couldn't reach the server."
      if (triggered) navigate(websiteId);
    },
    [navigate, triggerRun]
  );

  const addSite = useCallback(
    async (url: string): Promise<void> => {
      lastUrl.current = url;

      let websiteId: string;
      try {
        websiteId = (await createWebsite(url)).id;
      } catch (caught) {
        // `409` — already added. Go to it. No second run is triggered on this path: the
        // user asked for a site they already have, not for another crawl of it, and
        // starting one silently would spend a slot against their daily cap.
        const existing = isWebsiteAlreadyExists(caught);
        if (existing) {
          navigate(existing.website_id);
          return;
        }
        const { message, retryable } = describeFailure(caught);
        setError({ message, websiteId: null, retryable });
        return;
      }

      await startRun(websiteId);
    },
    [createWebsite, navigate, startRun]
  );

  const submit = useCallback(
    (input: string) => {
      const parsed = parseSiteUrl(input);
      if (!parsed.ok) {
        setError({ message: parsed.message, websiteId: null, retryable: false });
        return;
      }
      setError(null);
      void addSite(parsed.url);
    },
    [addSite]
  );

  const retry = useCallback(() => {
    if (!error?.retryable) return;
    const { websiteId } = error;
    setError(null);
    if (websiteId !== null) {
      void startRun(websiteId);
      return;
    }
    if (lastUrl.current !== null) void addSite(lastUrl.current);
  }, [addSite, error, startRun]);

  const clearError = useCallback(() => setError(null), []);

  return {
    submit,
    retry,
    clearError,
    error,
    isBusy: createWebsiteMutation.isPending || triggerRunMutation.isPending || isNavigating,
  };
}
