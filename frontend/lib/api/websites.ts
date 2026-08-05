// Named, feature-level wrappers around `api` (lib/api/fetcher.ts) for `/websites`. Every
// call site in the app — a page, a component, a React Query hook in `lib/query/` — should
// import from here rather than reaching for `api.get`/`api.post`/`api.delete` directly, so
// the endpoint list in this file, together with `lib/api/runs.ts` and `lib/api/health.ts`,
// is the entire inventory of what the frontend can ask the backend for.
//
// Read helpers for `GET /websites/{id}/runs` and `GET /runs/{id}` now live in
// `lib/api/runs.ts`, not here — the runs feature (PER-155) owns its own response shapes, the
// same way this file owns `WebsiteResponse` and friends. What is still ABSENT, from both
// files: `POST /websites/{id}/runs` (triggering a run), `GET`/`PUT /websites/{id}/schedule`,
// and any stats endpoint — see backend/app/api/routers/websites.py and
// backend/app/api/routers/runs.py, neither of which declares those routes yet. They land
// with their own tickets, each of which adds its own helpers here or in a sibling file.
// Writing `triggerRun`/`getSchedule` now, ahead of a real endpoint, would either hand-write a
// response shape nothing generates or silently point at a path `paths` does not know about —
// the latter is exactly what `PathsWithMethod` in fetcher.ts turns into a compile error,
// which is the intended guardrail working correctly, not a gap to work around.

import type { components } from "./schema";
import { api } from "./fetcher";

// Readable aliases so a call site never has to spell `components["schemas"]["..."]`
// itself. Each is a straight re-export of the generated type — never a hand-written
// shape — so a field the backend adds or removes shows up here automatically the next
// time `npm run gen:api` runs.
export type Website = components["schemas"]["WebsiteResponse"];
export type WebsiteListItem = components["schemas"]["WebsiteListItemResponse"];
export type LatestRun = components["schemas"]["LatestRunSummary"];
export type Schedule = components["schemas"]["ScheduleSummary"];
export type WebsiteAlreadyExistsDetail = components["schemas"]["WebsiteAlreadyExistsDetail"];

/**
 * `GET /websites`. Unfiltered by design (ARCHITECTURE.md §4.1) — every signed-in user sees
 * every website, and there is no `mine`-only variant to opt into here.
 *
 * `options.include: "latest_run"` is the only way `latest_run`/`schedule` are populated on
 * the returned rows; omit it and both fields come back `null` on every item.
 */
export function listWebsites(options?: { include?: "latest_run" }): Promise<WebsiteListItem[]> {
  return api.get("/websites", { query: { include: options?.include } });
}

/**
 * `GET /websites/{id}`. Carries no run or schedule information — see
 * `lib/query/use-website.ts` for why that means this endpoint's hook never polls.
 */
export function getWebsite(id: string): Promise<Website> {
  return api.get("/websites/{id}", { params: { id } });
}

/**
 * `POST /websites`. A `409` means the caller already has this origin registered — see
 * `lib/api/errors.ts`'s `isWebsiteAlreadyExists` for how a caller turns that into a
 * navigation instead of a dead-end error.
 */
export function createWebsite(url: string): Promise<Website> {
  return api.post("/websites", { body: { url } });
}

/** `DELETE /websites/{id}`. `403` if the caller is not the owner (ARCHITECTURE.md §4). */
export function deleteWebsite(id: string): Promise<void> {
  return api.delete("/websites/{id}", { params: { id } });
}
