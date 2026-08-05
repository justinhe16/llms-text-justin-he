// The single definition of "is a run still going" — every place in the frontend that needs
// to answer that question (today: whether to keep polling `GET /websites`, see
// lib/query/use-websites.ts) imports it from here rather than re-deriving it from a string
// comparison at the call site.

import type { components } from "./schema";
import type { WebsiteListItem } from "./websites";

/** The `runs.status` Postgres enum (db/schema.prisma), reached through the one response
 * DTO that currently exposes it (`LatestRunSummary` — see its own "THIS IS A TEMPORARY
 * HOME" note in backend/app/features/websites/schemas.py: the runs feature owns this
 * vocabulary once it lands). Derived, never hand-written, so a fifth status added to the
 * backend enum shows up here the next time `npm run gen:api` runs, rather than silently
 * missing from the `Record` below. */
export type RunStatus = components["schemas"]["LatestRunSummary"]["status"];

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
