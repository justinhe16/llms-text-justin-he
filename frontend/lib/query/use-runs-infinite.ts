"use client";

import { useInfiniteQuery, type InfiniteData } from "@tanstack/react-query";

import { anyRunActive } from "@/lib/api/run-status";
import { listRuns, type RunListItem, type RunListOptions, type RunPage } from "@/lib/api/runs";

import { pollWhileActive } from "./polling";
import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}/runs` as an accumulating, cursor-paginated list — the Runs tab's
 * "Load more" (components/crawls/runs-tab.tsx).
 *
 * `lib/query/use-runs.ts`'s `useRuns` is the sibling of this hook and is deliberately left
 * alone: it fetches exactly one page under a key that includes the cursor, which is the
 * right shape for "show me the latest N runs" and the wrong shape for a list a user
 * lengthens. Reusing it here would mean one cache entry per "Load more" click plus an
 * accumulation layer in the component to stitch them back together — and that layer is
 * precisely `useInfiniteQuery`, already written, already correct about the two things a
 * hand-rolled version gets wrong (see the polling note below).
 *
 * `filters` excludes `cursor` at the type level (`Omit<RunListOptions, "cursor">`). In an
 * infinite query the cursor is the page parameter, threaded by React Query through
 * `getNextPageParam`; a caller supplying one would be overriding the pagination from
 * outside it.
 */
export type RunListFilters = Omit<RunListOptions, "cursor">;

/**
 * Everything this query holds in the cache: the fetched pages plus the cursor each was
 * fetched with.
 *
 * The second type argument is not decoration. `InfiniteData<RunPage>` alone leaves the page
 * parameter as `unknown`, which does not match the `string | undefined` this query's
 * `initialPageParam` and `getNextPageParam` establish — and React Query compares the two
 * structurally deep inside `refetchInterval`'s type, so the mismatch surfaces as a
 * three-overload error naming `QueryBehavior` rather than the one word (`pageParam`) that
 * is actually wrong. Naming the pair once, here, keeps that from being rediscovered at
 * every use.
 */
export type RunPages = InfiniteData<RunPage, string | undefined>;

/**
 * Whether any run on *any* loaded page is still in progress.
 *
 * A fold of `anyRunActive` (lib/api/run-status.ts) over the pages, which is itself a fold
 * of `isActiveRunStatus` over one page's rows — so this adds a third shape to the list that
 * shares one definition of "still running" and does not add a second definition of it. The
 * fold lives here rather than in `lib/api/run-status.ts` on purpose: `InfiniteData` is a
 * React Query type, and the `lib/api/` layer describes the backend's shapes without knowing
 * what this app caches them in.
 */
function anyRunActiveInPages(data: RunPages): boolean {
  return data.pages.some(anyRunActive);
}

/**
 * Every loaded run, flattened across pages, newest first — the array the table renders.
 *
 * Exported as a plain function rather than folded into the hook's return so a caller that
 * wants the pages themselves still has them, and so this is testable without a React tree.
 */
export function flattenRunPages(data: RunPages | undefined): RunListItem[] {
  return data?.pages.flatMap((page) => page.items) ?? [];
}

/**
 * The website's run history, one page at a time, polling while anything on any loaded page
 * is still running.
 *
 * Two properties worth stating explicitly, because both are easy to get wrong by hand and
 * are the reason this is a `useInfiniteQuery` rather than a `useQuery` with a `useState`
 * array beside it:
 *
 * - **A poll refetches every loaded page, not just the first.** That is React Query's own
 *   behavior for an infinite query, and it is the correct one here: a `processing` run four
 *   pages down still has to flip to `completed` on screen. A hand-rolled accumulator would
 *   have refetched page 1 and left the rest frozen at whatever they said when they loaded.
 *   The cost is real but bounded and self-limiting — polling only happens while a run is
 *   active (`pollWhileActive`), a user has to click "Load more" to add pages, and active
 *   runs are always the *newest* rows, so in practice the tick is one request. It also
 *   stops entirely on a hidden tab (`refetchIntervalInBackground: false`, app/providers.tsx).
 * - **A refetch cannot tear the list.** React Query replays the accumulated `pageParams`
 *   in order, so the list is rebuilt from the same cursors rather than re-derived from
 *   whatever `next_cursor` happens to say mid-crawl. Keyset cursors over `started_at`
 *   (backend/app/core/pagination.py) make that stable even as new runs are inserted at the
 *   head: a new run changes page 1's contents and shifts nothing after it.
 */
export function useRunsInfinite(websiteId: string, filters?: RunListFilters) {
  // All five type arguments spelled out — `<TQueryFnData, TError, TData, TQueryKey,
  // TPageParam>` — rather than left to inference. `useInfiniteQuery`'s own default for
  // `TData` is `InfiniteData<TQueryFnData>`, which drops the page-param type on the floor
  // (it defaults to `unknown`), so an inferred call hands callers a `data` whose
  // `pageParams` are `unknown[]` while every option on this object agrees they are
  // `(string | undefined)[]`. Nothing inside the hook notices; the mismatch lands on
  // whoever passes `data` to `flattenRunPages`, as an error about `QueryBehavior`.
  return useInfiniteQuery<RunPage, Error, RunPages, QueryKey, string | undefined>({
    queryKey: queryKeys.runs.infinite(websiteId, filters),
    queryFn: ({ pageParam }) => listRuns(websiteId, { ...filters, cursor: pageParam }),

    // The first page asks for no cursor at all — `listRuns` drops an `undefined` query
    // value rather than sending `?cursor=undefined` (lib/api/fetcher.ts's
    // `buildQueryString`), so this is genuinely "start from the newest run."
    initialPageParam: undefined as string | undefined,

    // `?? undefined`, not `?? null`: React Query treats `undefined` (and only `undefined`)
    // as "there is no next page", which is what drives `hasNextPage` and therefore whether
    // the Runs tab renders its "Load more" button at all. `next_cursor` is `string | null`
    // on the wire — returning that `null` through would leave `hasNextPage` true forever
    // and give the user a button that fetches the first page again on every click.
    getNextPageParam: (lastPage: RunPage) => lastPage.next_cursor ?? undefined,

    // Both type arguments written out, and they are genuinely different here: one fetch
    // returns a `RunPage`, the cache holds an `InfiniteData<RunPage>`, and React Query's
    // `refetchInterval` callback is typed over both. Leaving either to inference makes
    // TypeScript resolve `TQueryFnData` to the *cached* type and then reject `queryFn`,
    // `getNextPageParam`, and this option together with one unreadable overload error.
    refetchInterval: pollWhileActive<RunPage, RunPages>(anyRunActiveInPages),
  });
}
