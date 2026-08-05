"use client";

import { useQuery } from "@tanstack/react-query";

import { runIsActive } from "@/lib/api/run-status";
import { getRun, type RunDetail } from "@/lib/api/runs";

import { pollWhileActive } from "./polling";
import { queryKeys } from "./query-keys";

/**
 * `GET /runs/{id}`, kept fresh while the run is still in progress — the "detail header" case
 * this hook exists for: a single run's own page, where the header (status, duration, error)
 * needs to update live while the crawl is still going.
 *
 * Polls on `runIsActive` (lib/api/run-status.ts), which is the exact same `isActiveRunStatus`
 * `useRuns` above folds over a page of runs and `useWebsites` folds over a website list —
 * this hook just hands it one run instead of a collection. That is the payoff this ticket
 * shipped for: one predicate, reused across three different data shapes, rather than a
 * subtly different "is it done yet" check invented at each call site.
 */
export function useRun(id: string) {
  return useQuery({
    queryKey: queryKeys.runs.detail(id),
    queryFn: () => getRun(id),
    // `pollWhileActive<RunDetail>`, with the type argument written out rather than
    // inferred. `runIsActive` takes a `RunListItem`, this query's data is a `RunDetail`
    // (the same fields plus `llms_txt`/`storage_path`), and leaving inference to it makes
    // TypeScript pick `TData = RunListItem` from the predicate's parameter and then fail to
    // reconcile that with the `Query<RunDetail>` `useQuery` actually hands the callback.
    // Pinning `TData` to the query's own type fixes it in the honest direction: parameters
    // are checked contravariantly, so a predicate that only needs `RunListItem`'s fields is
    // a perfectly valid predicate over `RunDetail`, and this asks the compiler to check
    // exactly that. Widening `runIsActive` to accept `RunDetail` would be the wrong fix —
    // it reads more of the run than it needs and would stop working for `useRuns`' rows.
    refetchInterval: pollWhileActive<RunDetail>(runIsActive),
  });
}
