// Named, feature-level wrappers around `api` (lib/api/fetcher.ts) for
// `/websites/{id}/schedule`. Every call site in the app — a page, a component, a React
// Query hook in `lib/query/` — should import from here rather than reaching for
// `api.get`/`api.put` directly, so the endpoint list in this file, together with
// lib/api/websites.ts, lib/api/runs.ts, and lib/api/health.ts, is the entire inventory of
// what the frontend can ask the backend for.
//
// This file owns the *full* schedule shape (`ScheduleResponse`, aliased below as
// `Schedule`) — not to be confused with `ScheduleSummary` in lib/api/websites.ts, the
// compact fold `GET /websites?include=latest_run` embeds in each row. They are two
// genuinely different generated types (see that file's comment on `ScheduleSummary`), and
// keeping only one of them named `Schedule` — this one, since it is the type a settings
// page reads and writes against — is the whole point of calling this out here rather than
// letting both files quietly claim the same name.
//
// `auto_publish` is deliberately not exposed anywhere in this file, in either direction.
// `ScheduleResponse`'s own docstring (backend/app/features/schedules/schemas.py) explains
// why at length: the column is reserved for a later milestone, and even a read-only field
// today would let a client start depending on a value nothing in this system ever sets to
// anything but its default. There is nothing to "add" here later so much as a new field to
// generate the day that milestone ships — this file should not anticipate it.
//
// `getStats(id, window)` is still ABSENT — `GET /websites/{id}/stats` shipped with PER-156,
// on backend/app/api/routers/runs.py, but no typed helper or Trends tab reads it yet; see
// lib/api/websites.ts's header comment for the rest of what's still missing.

import type { components } from "./schema";
import { api } from "./fetcher";

// Readable aliases so a call site never has to spell `components["schemas"]["..."]`
// itself. Each is a straight re-export of (or indexed access into) the generated type —
// never a hand-written shape — so a field the backend adds or removes shows up here
// automatically the next time `npm run gen:api` runs.
export type Schedule = components["schemas"]["ScheduleResponse"];
export type UpsertScheduleRequest = components["schemas"]["UpsertScheduleRequest"];

// The four UI presets this product ships, reached through `UpsertScheduleRequest` rather
// than re-listed as a literal union here — see `ScheduleIntervalMinutes` in
// backend/app/features/schedules/schemas.py for why the backend expresses it the same way
// (a `Literal`, not a bare `int`), and why `Schedule["interval_minutes"]` above is *not*
// this type: the response widens to a plain `number` on purpose, and narrowing it back
// would be "fixing" an asymmetry the backend introduced deliberately. A UI preset picker
// (hourly/6-hourly/daily/weekly) should type its options from this alias, so a fifth preset
// added upstream shows up here on the next `npm run gen:api` instead of silently being
// missing from a hand-copied `60 | 360 | 1440 | 10080`.
export type ScheduleIntervalMinutes = UpsertScheduleRequest["interval_minutes"];

/**
 * `GET /websites/{id}/schedule`. Unfiltered by caller identity, like every read in this
 * codebase (ARCHITECTURE.md §4) — any signed-in user may read any website's schedule.
 * `404` if `id` names no website.
 *
 * Returns `null`, not a thrown `ApiError`, when the website has no schedule configured yet.
 * That is the OpenAPI document's own answer (`ScheduleResponse | null`, a genuine `anyOf`,
 * not this client papering over an edge case), and `getSchedule`'s return type says so
 * directly rather than collapsing "no schedule" into an error a caller has to catch. See
 * `get_schedule` in backend/app/api/routers/schedules.py for why: a `200` with a `null`
 * body is the normal "nothing configured" case, and `404` is reserved for an unknown
 * *website*, never for a missing schedule.
 */
export function getSchedule(websiteId: string): Promise<Schedule | null> {
  return api.get("/websites/{id}/schedule", { params: { id: websiteId } });
}

/**
 * `PUT /websites/{id}/schedule`. Owner only — `403` otherwise (ARCHITECTURE.md §4) — and
 * always upserts: there is no separate create-vs-update call from this client, matching the
 * single service method behind the route (`upsert_schedule` in
 * backend/app/api/routers/schedules.py). Always resolves `200`; there is no conditional
 * `201` for the create case to branch on here.
 *
 * `body.interval_minutes` is typed as `ScheduleIntervalMinutes` above, so a caller that
 * passes anything outside the four supported presets fails at `tsc`, not as a `422` a
 * caller only discovers at runtime.
 */
export function putSchedule(websiteId: string, body: UpsertScheduleRequest): Promise<Schedule> {
  return api.put("/websites/{id}/schedule", { params: { id: websiteId }, body });
}
