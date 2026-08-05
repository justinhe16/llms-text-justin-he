// Named, feature-level wrappers around `api` (lib/api/fetcher.ts) for `/websites/{id}/runs`
// and `/runs/{id}`. Every call site in the app — a page, a component, a React Query hook in
// `lib/query/` — should import from here rather than reaching for `api.get` directly, so the
// endpoint list in this file, together with lib/api/websites.ts, lib/api/schedules.ts, and
// lib/api/health.ts, is the entire inventory of what the frontend can ask the backend for.
//
// A trigger helper (`POST /websites/{id}/runs`) and any stats endpoint are deliberately
// ABSENT. The runs feature (PER-155) shipped only its two read routes — see
// backend/app/api/routers/runs.py, which declares exactly `GET /websites/{id}/runs` and `GET
// /runs/{id}` and no more — and stats (PER-156) has not landed on the websites router either.
// Schedules are no longer in that list: `GET`/`PUT /websites/{id}/schedule` shipped with
// PER-154 and live in `lib/api/schedules.ts`, not here or in `lib/api/websites.ts` — the
// schedules feature owns its own response shapes, the same way this file owns
// `RunListItemResponse` and friends. The remaining absences land with their own tickets,
// each of which adds its own helper here or in a sibling file. Writing `triggerRun`/
// `getStats` now, ahead of a real endpoint, would either hand-write a response shape nothing
// generates or silently point at a path `paths` does not know about — the latter is exactly
// what `PathsWithMethod` in fetcher.ts turns into a compile error, which is the intended
// guardrail working correctly, not a gap to work around.

import type { components } from "./schema";
import { api } from "./fetcher";

// Readable aliases so a call site never has to spell `components["schemas"]["..."]` itself.
// Each is a straight re-export of the generated type — never a hand-written shape — so a
// field the backend adds or removes shows up here automatically the next time
// `npm run gen:api` runs.
export type RunListItem = components["schemas"]["RunListItemResponse"];
export type RunDetail = components["schemas"]["RunDetailResponse"];
export type RunPage = components["schemas"]["Page_RunListItemResponse_"];
export type RunTrigger = components["schemas"]["RunListItemResponse"]["trigger"];

/**
 * The optional query parameters `GET /websites/{id}/runs` accepts. Defined once here, rather
 * than re-typed at each call site, because `listRuns` below, `lib/query/use-runs.ts`'s
 * `useRuns`, and `lib/query/query-keys.ts`'s `runs.list` all need the *same* shape — a cache
 * key that can drift from the arguments a query actually fetched with is worse than no cache
 * key at all. `status` is spelled as `RunListItem["status"]` rather than a second, hand-copied
 * copy of the enum; see `lib/api/run-status.ts`'s `RunStatus` for the same value reached
 * through the vocabulary every "is this run active" predicate in this app already shares.
 */
export type RunListOptions = {
  cursor?: string;
  status?: RunListItem["status"];
  limit?: number;
};

/**
 * `GET /websites/{id}/runs`. Unfiltered by caller identity, like every read in this codebase
 * (ARCHITECTURE.md §4.1) — any signed-in user may page through any website's run history.
 * `404` if `websiteId` names no website.
 *
 * Cursor pagination, not `?page=`: `options.cursor` is meant to be the opaque `next_cursor`
 * a previous call returned (`RunPage["next_cursor"]`) and nothing this client constructs —
 * see `backend/app/core/pagination.py`'s module docstring for why offset pagination is wrong
 * for this list specifically.
 */
export function listRuns(websiteId: string, options?: RunListOptions): Promise<RunPage> {
  return api.get("/websites/{id}/runs", {
    params: { id: websiteId },
    query: { cursor: options?.cursor, status: options?.status, limit: options?.limit },
  });
}

/**
 * `GET /runs/{id}`. Unfiltered by caller identity (ARCHITECTURE.md §4.1) — any signed-in
 * user may read any run's full detail, including `llms_txt`. `404` if `id` names no run.
 */
export function getRun(id: string): Promise<RunDetail> {
  return api.get("/runs/{id}", { params: { id } });
}
