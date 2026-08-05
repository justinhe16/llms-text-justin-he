// The single definition of "is a run still going" — every place in the frontend that needs
// to answer that question imports it from here rather than re-deriving it from a string
// comparison at the call site. Three call sites share this one predicate today:
// `anyWebsiteHasActiveRun` for the websites list (`lib/query/use-websites.ts`, `GET
// /websites?include=latest_run`), `anyRunActive` for a website's run history
// (`lib/query/use-runs.ts`, `GET /websites/{id}/runs`), and `runIsActive` for a single run's
// own detail (`lib/query/use-run.ts`, `GET /runs/{id}`). All three funnel through
// `isActiveRunStatus`, so "is this status still in progress" is decided in exactly one place
// no matter which response shape is asking.

import type { RunListItem, RunPage } from "./runs";
import type { WebsiteListItem } from "./websites";

/**
 * The `runs.status` Postgres enum (db/schema.prisma), reached through `RunListItemResponse`
 * — the runs feature's own DTO (`backend/app/features/runs/schemas.py`), which is where
 * `RunStatusName` is now defined. It used to live on `websites/schemas.py`'s
 * `LatestRunSummary` under a "THIS IS A TEMPORARY HOME" note; that note is resolved now that
 * the runs feature exists, and `websites/schemas.py` imports `RunStatusName` from there
 * rather than defining a second copy. Derived, never hand-written, so a fifth status added
 * to the backend enum shows up here the next time `npm run gen:api` runs, rather than
 * silently missing from the `Record` below.
 *
 * `LatestRunSummary.status` (folded into `GET /websites`) is typed from that very same
 * `RunStatusName`, so it is structurally identical to this type today — see
 * `schema.type-test.ts`'s `AssertWebsiteRunStatusMatchesRunsRunStatus` for the compile-time
 * check that keeps it that way. If the backend ever lets the two drift, that assertion fails
 * `tsc`, rather than `websiteHasActiveRun` and `runIsActive` silently answering "is this
 * active" two different ways for what was supposed to be the same vocabulary.
 */
export type RunStatus = RunListItem["status"];

/**
 * Whether a run in this status is still doing something — i.e. worth polling for.
 *
 * Spelled as an exhaustive `Record<RunStatus, boolean>`, not a function with an
 * `if`/`else` or a `Set.has()`, because a `Record` missing a key is a `tsc` error: if
 * Postgres ever grows a fifth `run_status` value, `RunStatus` above picks it up on the
 * next `npm run gen:api`, and this object then fails to compile until someone decides,
 * explicitly, whether the new status is active or terminal. A `switch` with a
 * `default: return false` would instead make that decision silently, and wrongly, for
 * every status added after this file was written.
 */
const ACTIVE_BY_STATUS: Record<RunStatus, boolean> = {
  pending: true,
  processing: true,
  completed: false,
  failed: false,
};

/** Whether `status` describes a run that has not finished yet. */
export function isActiveRunStatus(status: RunStatus): boolean {
  return ACTIVE_BY_STATUS[status];
}

/**
 * Whether `website.latest_run` — present only when `GET /websites` was called with
 * `?include=latest_run` (`lib/api/websites.ts`'s `listWebsites`) — describes a run still
 * in progress. A website with no `latest_run` (never requested, or genuinely has no runs
 * yet) is not active; there is nothing to distinguish those two cases here, and nothing
 * that needs to.
 */
export function websiteHasActiveRun(website: WebsiteListItem): boolean {
  return website.latest_run !== null && website.latest_run !== undefined
    ? isActiveRunStatus(website.latest_run.status)
    : false;
}

/** Whether any website in the list has a run in progress — the predicate
 * `lib/query/use-websites.ts` polls on. */
export function anyWebsiteHasActiveRun(websites: WebsiteListItem[]): boolean {
  return websites.some(websiteHasActiveRun);
}

/**
 * Whether one run is still in progress — a `GET /runs/{id}` response or a row of `GET
 * /websites/{id}/runs`. `RunDetail` (lib/api/runs.ts) satisfies this structurally: it has
 * every field `RunListItem` does, plus two (`llms_txt`, `storage_path`) this function never
 * reads. The predicate `lib/query/use-run.ts`'s `useRun` polls a single run's detail on.
 */
export function runIsActive(run: RunListItem): boolean {
  return isActiveRunStatus(run.status);
}

/** Whether any run on a page of `GET /websites/{id}/runs` is still in progress — the
 * predicate `lib/query/use-runs.ts`'s `useRuns` polls a website's run history on. */
export function anyRunActive(page: RunPage): boolean {
  return page.items.some(runIsActive);
}
