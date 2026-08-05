// The one place a poll interval is decided. `use-websites.ts` is the only current caller,
// but the rule — "poll while something in the result is still in progress, stop the moment
// it isn't, and never poll a tab nobody is looking at" — is a product decision independent
// of which query it applies to, so it is written once here rather than inlined into a
// `useQuery` call.

import type { Query, QueryKey } from "@tanstack/react-query";

/** How often an active query refetches. Not configurable per call site: every consumer of
 * `pollWhileActive` shares one polling cadence, and a second value would need its own
 * justification (a run genuinely completing faster or slower than any other resource in
 * this app does not exist yet). */
export const ACTIVE_POLL_INTERVAL_MS = 3_000;

/**
 * Builds a `refetchInterval` callback for `useQuery` from a predicate over that query's
 * own data: `true` while the predicate holds means "poll every
 * `ACTIVE_POLL_INTERVAL_MS`", and `false` means "stop" — TanStack Query re-evaluates this
 * function after every fetch, so a query that was active on one response and terminal on
 * the next stops polling itself without anything downstream having to notice.
 *
 * Two properties this depends on, both required at the `useQuery` call site and worth
 * restating here because neither is visible from this function's signature alone:
 *
 * - **Polling stops at a terminal state, on its own.** That is this function's entire
 *   contract — `isActive` is the single shared predicate a caller passes in (e.g.
 *   `anyWebsiteHasActiveRun` from `lib/api/run-status.ts`), so "what counts as still
 *   running" is decided in exactly one place for every query that polls.
 * - **Polling pauses on a hidden tab.** This function has no way to enforce that —
 *   `refetchIntervalInBackground` is a sibling option, not a return value this callback
 *   controls — which is why `app/providers.tsx` sets `refetchIntervalInBackground: false`
 *   as a `QueryClient` default rather than trusting every call site to repeat it. A
 *   background tab polling every three seconds for as long as a run takes to finish is a
 *   real resource cost with no user watching it happen, and it is exactly the kind of
 *   default that is easy to reintroduce by omission if it is ever left to individual
 *   `useQuery` calls instead.
 *
 * `query.state.data` is `undefined` before the first successful fetch, which this treats
 * as "nothing to poll yet" rather than calling `isActive(undefined)` — every current
 * `isActive` predicate expects real data (an array of websites, say) and has no defined
 * answer for "no data at all."
 */
export function pollWhileActive<TData>(
  isActive: (data: TData) => boolean
): (query: Query<TData, Error, TData, QueryKey>) => number | false {
  return (query) => {
    const data = query.state.data;
    return data !== undefined && isActive(data) ? ACTIVE_POLL_INTERVAL_MS : false;
  };
}
