// Named, feature-level wrappers around `api` (lib/api/fetcher.ts) for `/websites/{id}/runs`
// and `/runs/{id}`. Every call site in the app — a page, a component, a React Query hook in
// `lib/query/` — should import from here rather than reaching for `api.get` directly, so the
// endpoint list in this file, together with lib/api/websites.ts, lib/api/schedules.ts, and
// lib/api/health.ts, is the entire inventory of what the frontend can ask the backend for.
//
// `triggerRun` (`POST /websites/{id}/runs`) is no longer on the absent list: PER-160 shipped
// the route, and PER-162's "Run now" button is the caller that needed it. It is the only
// WRITE in this file — the two reads above it are unfiltered by caller identity, this one is
// owner-only, and the four error shapes it can return are the reason `RunAlreadyInFlightDetail`
// and `RunLimitExceededDetail` are exported below.
//
// Still ABSENT: `GET /websites/{id}/stats` (PER-156), which landed on
// backend/app/api/routers/runs.py alongside the two reads here but has no `getStats` helper
// yet — see lib/api/websites.ts's header comment for why writing one ahead of the Trends tab
// that would call it would be premature. Schedules are not on the list at all: `GET`/`PUT
// /websites/{id}/schedule` shipped with PER-154 and live in `lib/api/schedules.ts` — the
// schedules feature owns its own response shapes, the same way this file owns
// `RunListItemResponse` and friends. Writing `getStats` now would either hand-write a
// response shape nothing generates or silently point at a path `paths` does not know about —
// the latter is exactly what `PathsWithMethod` in fetcher.ts turns into a compile error,
// which is the intended guardrail working correctly, not a gap to work around.

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
 * The `202` body of `POST /websites/{id}/runs` — `{ id, status, started_at }` and nothing
 * else. Named `TriggeredRun` rather than mirroring the backend's `TriggerRunResponse`
 * because `RunTrigger` above is already taken by the `manual`/`scheduled` enum, and two
 * exports one character apart meaning entirely different things is exactly the kind of
 * drift the aliases in this file exist to prevent.
 *
 * Deliberately *not* a `RunListItem`: a just-queued run has no `completed_at`,
 * `duration_ms`, `stats`, or `error` worth returning (see `TriggerRunResponse`'s own
 * docstring in backend/app/features/runs/schemas.py). A caller that wants those fields
 * reads the run back through `listRuns`/`getRun`, which is what polling does anyway.
 */
export type TriggeredRun = components["schemas"]["TriggerRunResponse"];

/**
 * The `detail` of the `409` from `POST /websites/{id}/runs` — the website already has a
 * `pending` or `processing` run, and this carries that run's id so the UI can navigate to
 * it instead of dead-ending on an error. Read it through
 * `lib/api/errors.ts`'s `isRunAlreadyInFlight`, never by hand off `ApiError.body`.
 */
export type RunAlreadyInFlightDetail = components["schemas"]["RunAlreadyInFlightDetail"];

/**
 * The `detail` of the `429` from `POST /websites/{id}/runs` — either the per-user
 * concurrency cap (`scope: "concurrent"`, `resets_at: null`) or the rolling-24h daily cap
 * (`scope: "daily"`, `resets_at` set). Read it through `lib/api/errors.ts`'s
 * `isRunLimitExceeded`.
 */
export type RunLimitExceededDetail = components["schemas"]["RunLimitExceededDetail"];

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

/**
 * `POST /websites/{id}/runs` — queue a manual crawl. The one write in this file, and the
 * only one of its three functions that is ownership-checked (ARCHITECTURE.md §4.2): the
 * two reads above are unfiltered by caller identity, this one is owner-only.
 *
 * `202 Accepted`, not `201`: the returned run is `pending` and has only been *queued*. The
 * caller's job after this resolves is to let `lib/query/use-runs-infinite.ts`'s polling
 * take over, not to expect a finished crawl.
 *
 * Five failures a caller has to be ready for, each with its own meaning — see
 * `lib/query/use-trigger-run.ts`, which is where the branching actually lives:
 *
 * - `403` — not the owner. The UI gates the button on ownership, so this is the
 *   belt-and-braces case (a stale `user_id`, a second tab signed in as someone else).
 * - `404` — no such website.
 * - `409` — this website already has a run in flight. `RunAlreadyInFlightDetail` carries
 *   that run's id; `lib/api/errors.ts`'s `isRunAlreadyInFlight` is how you get at it.
 * - `429` — over a per-user cap. `RunLimitExceededDetail` carries `scope`, `limit`, and
 *   (daily only) `resets_at`; the `message` is prose worth surfacing verbatim.
 * - `503` — the run row was written but could not be queued. Distinct from a generic
 *   failure: the crawl genuinely will not happen, and retrying later is the right advice.
 */
export function triggerRun(websiteId: string): Promise<TriggeredRun> {
  return api.post("/websites/{id}/runs", { params: { id: websiteId } });
}
