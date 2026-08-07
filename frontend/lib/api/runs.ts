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
// `getStats` (`GET /websites/{id}/stats`) is no longer absent either: PER-156 shipped the
// route on backend/app/api/routers/runs.py alongside the two reads above, and PER-169's
// Trends tab is the caller that needed it. It lives in THIS file rather than
// lib/api/websites.ts despite its `/websites/…` path, for the same reason the backend put
// the route on its runs router: what it returns is run statistics — counts, durations and
// page averages of runs — and the website id in the path is only the scope. Schedules are
// not on the list at all: `GET`/`PUT /websites/{id}/schedule` shipped with PER-154 and live
// in `lib/api/schedules.ts` — the schedules feature owns its own response shapes, the same
// way this file owns `RunListItemResponse` and friends.
//
// `getLlmsTxt` (`GET /runs/{id}/llms.txt`) and `getLlmsFullTxt` (`GET /runs/{id}/llms-full.txt`)
// round out the pair PER-181 shipped alongside `getRun` above: each downloads one of a run's
// two artifacts as `text/plain` rather than a JSON body, which is exactly the case
// `lib/api/fetcher.ts`'s `SuccessBodyOf` grew a second branch for, so `api.get` still resolves
// each call to `Promise<string>` instead of silently to `Promise<void>`. PER-182's "Download
// full" button (`components/crawls/download-full-button.tsx`, via
// `lib/crawls/use-download-full-artifact.ts`) is the caller that needed `getLlmsFullTxt`.
// `getLlmsTxt` has no caller yet — the Output tab's existing Download button already holds
// `llms_txt` in memory from `GET /runs/{id}` and builds its `Blob` from that, with no request
// of its own — and it is listed here honestly rather than pretending otherwise, because this
// file's inventory of what the frontend can ask `/runs/{id}/…` for is meant to be complete.
//
// Nothing is on the absent list any more. When the next endpoint lands, add its helper here
// or in the sibling file that owns its feature — never a hand-written response shape, and
// never a path `paths` does not know about, which `PathsWithMethod` in fetcher.ts turns into
// a compile error rather than a runtime 404.

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

// ---- Stats (`GET /websites/{id}/stats`, PER-156) ---------------------------------------

/** The whole `GET /websites/{id}/stats` body: the echoed `window`/`bucket`, the `series`, and
 * the whole-window `totals`. */
export type WebsiteStats = components["schemas"]["WebsiteStatsResponse"];

/**
 * One bucket of `series`.
 *
 * Every bucket in the window is present exactly once, **including buckets with no runs**,
 * which report zeroes rather than being omitted — the backend zero-fills them with
 * `generate_series` (backend/app/features/runs/internals/runs_reader.py). `avg_pages` and
 * `avg_duration_ms` are therefore `number`, never `null`, and a zero in either is a real
 * measurement of a quiet hour rather than missing data. A chart must render those zeroes as
 * zeroes and must not interpolate across them.
 */
export type StatsPoint = components["schemas"]["RunStatsPoint"];

/**
 * The whole-window summary.
 *
 * `success_rate` is the field to be careful with. It is a **fraction in `[0, 1]`**, not a
 * percentage, and it is `null` — never `0` — when the window contains no runs at all
 * (`service.py`'s `_to_stats`: `total_completed / total_runs if total_runs > 0 else None`).
 * The backend draws that distinction deliberately, so "nothing ran" and "everything failed"
 * cannot be confused, and a caller must read it with `??` rather than `||`: `0 || "—"` shows
 * a dash for a genuine 0% success rate, which is precisely backwards.
 *
 * `last_run_at` is scoped to the window like everything else here, so it is `null` both for a
 * website that has never run and for one whose runs are all older than the window. It cannot
 * tell those two apart; a caller that needs to (the Trends tab does, to choose between two
 * empty states) has to look at the run history instead.
 */
export type StatsTotals = components["schemas"]["RunStatsTotals"];

/**
 * The newest completed run inside the requested window, and what changed in its index —
 * `null` when the window contains no completed run at all (PER-193).
 *
 * **Window-scoped, like `StatsTotals.last_run_at`** — a completed run older than the window
 * does not surface here, so `null` does not mean "this website has never completed a run."
 *
 * `diff_state` is the discriminator a caller branches on, never `diff === null` on its own
 * and never whether any individual count is `0` — see `TrendsWhatChanged`
 * (components/crawls/trends-what-changed.tsx) for the five-branch state table this field
 * drives.
 */
export type StatsLatest = components["schemas"]["LatestRunSnapshot"];

/**
 * What changed in the latest run's index, compared with the previous completed run's.
 * Present on `StatsLatest.diff` if and only if `diff_state === "compared"`.
 */
export type StatsIndexDiff = components["schemas"]["RunIndexDiff"];

/** One page in a sample list (`StatsIndexDiff.added_sample` and its two siblings). */
export type StatsIndexPageRef = components["schemas"]["IndexPageRef"];

/**
 * The three windows `?window=` accepts. Read off the *response* type rather than the request
 * parameter because the response echoes the resolved window back and the two unions are the
 * same by construction — and this spelling stays a one-hop alias of generated code, which a
 * hand-written `"1d" | "7d" | "14d"` would not.
 */
export type StatsWindow = WebsiteStats["window"];

/**
 * The bucket size the backend chose for a window — `hour` for 1d and 7d, `day` for 14d.
 *
 * Derived from `window` by the server (`internals/stats_window.py`) and never supplied by the
 * caller, which is exactly why it is echoed back: a client formats its axis from **this
 * field**, not from a second copy of the window→bucket mapping that could disagree with the
 * one that actually produced the data.
 */
export type StatsBucket = WebsiteStats["bucket"];

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

/**
 * `GET /websites/{id}/stats?window=`. Unfiltered by caller identity like the two reads above
 * (ARCHITECTURE.md §4.1) — any signed-in user may read any website's statistics.
 *
 * `window` is required *here* although the endpoint defaults it to `7d` server-side. The
 * default is not the interesting part: the caller (lib/query/use-website-stats.ts) puts the
 * window in the React Query cache key, and a helper that let it be omitted would make
 * "fetched with no window" and "fetched with 7d" two keys for one response. Deciding the
 * default in one place — `DEFAULT_STATS_WINDOW` in lib/crawls/use-detail-view.ts, which reads
 * it off the URL — keeps the key and the request describing the same request.
 *
 * `404` if `id` names no website. An unrecognized `window` is a `422`, but `StatsWindow`
 * makes that unreachable from TypeScript.
 */
export function getStats(websiteId: string, window: StatsWindow): Promise<WebsiteStats> {
  return api.get("/websites/{id}/stats", { params: { id: websiteId }, query: { window } });
}

// ---- Artifact downloads (`GET /runs/{id}/llms.txt`, `GET /runs/{id}/llms-full.txt`, PER-181) -

/**
 * `GET /runs/{id}/llms.txt`. Unfiltered by caller identity (ARCHITECTURE.md §4.1) — any
 * signed-in user may download any run's index. Two distinguishable `404`s share the one
 * status code: no run exists with `id`, or that run exists but has not produced this
 * artifact — still `pending`/`processing`, or it `failed` — and the two cases answer with
 * different `detail` strings (`RunService.get_llms_txt`).
 *
 * No caller yet, and that is worth saying plainly rather than leaving this looking dead: the
 * Output tab's Download button already holds `llms_txt` in memory from `GET /runs/{id}` and
 * builds its `Blob` from that content directly, so it never needs this route. This helper
 * exists so the inventory above stays complete — it is the other half of the pair
 * `getLlmsFullTxt` below completes.
 */
export function getLlmsTxt(id: string): Promise<string> {
  return api.get("/runs/{id}/llms.txt", { params: { id } });
}

/**
 * `GET /runs/{id}/llms-full.txt`. Unfiltered by caller identity (ARCHITECTURE.md §4.1), the
 * same contract as `getLlmsTxt` above, including its two distinguishable `404`s — no such
 * run, or that run has not produced this artifact yet — see `RunService.get_llms_full_txt`.
 *
 * `llms_full_txt` is deliberately absent from `RunDetail`/`RunDetailResponse`: `useRun`
 * (lib/query/use-run.ts) polls `GET /runs/{id}` every three seconds while a run is active, and
 * riding a second, potentially large artifact along on every poll tick would be wasted
 * bandwidth for a tab nobody has opened. This route is the only way to fetch it, and
 * `useDownloadFullArtifact` (lib/crawls/use-download-full-artifact.ts) is the one caller — the
 * Output tab's "Download full" button.
 */
export function getLlmsFullTxt(id: string): Promise<string> {
  return api.get("/runs/{id}/llms-full.txt", { params: { id } });
}
