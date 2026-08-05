// The one place React Query cache keys are constructed. Every hook in this directory
// imports `queryKeys` rather than writing its own array literal, so invalidating "every
// website list" or "this one website" means calling a function here instead of hoping
// every call site agrees, by convention, on the same shape.
//
// `as const` throughout: without it, `["websites", "list", include]` widens to
// `(string | undefined)[]`, and React Query keys are compared by value, so two calls that
// meant the same key but got different inferred array types would still work at runtime —
// but a query key is exactly the kind of "must be structurally identical every time" value
// this repo elsewhere reaches for a literal type to protect (see `RunStatus` in
// lib/api/run-status.ts for the same instinct applied to a different problem).

import type { RunListOptions } from "@/lib/api/runs";

const websites = {
  // The root of every website-related key. `queryClient.invalidateQueries({ queryKey:
  // queryKeys.websites.all })` matches this and everything nested under it (the `list` and
  // `detail` keys below), which is the coarse "something about websites changed"
  // invalidation `lib/query/use-create-website.ts` and `use-delete-website.ts` both use.
  all: ["websites"] as const,

  // `include` is part of the key because a fetch with `?include=latest_run` and one
  // without return genuinely different shapes of the same rows (lib/api/websites.ts's
  // `listWebsites`) — caching them under one key would let a component that never asked
  // for `latest_run` read a stale response that happened to have it, or vice versa.
  list: (include?: "latest_run") => ["websites", "list", include] as const,

  detail: (id: string) => ["websites", "detail", id] as const,
};

const runs = {
  // The root of every run-related key, mirroring `websites.all` above — coarse enough to
  // invalidate every run list and every run detail currently mounted, for the rare change
  // that could affect any of them at once.
  all: ["runs"] as const,

  // `websiteId` scopes the key to one website's run history; `options` (cursor, status,
  // limit — `RunListOptions`, lib/api/runs.ts) is part of it for the same reason `include`
  // is part of `websites.list` above: two different option sets are two different pages or
  // filters of the same underlying list, and caching them under one key would let a
  // component reading one see a response fetched for the other.
  list: (websiteId: string, options?: RunListOptions) =>
    ["runs", "list", websiteId, options] as const,

  // One run's own detail (`GET /runs/{id}`) — independent of which website's history it was
  // reached from, since a run's id alone identifies it.
  detail: (id: string) => ["runs", "detail", id] as const,
};

// A schedule has no independent id in the API surface — it is 1:1 with a website, reached
// only via `/websites/{id}/schedule` — so unlike `runs.detail` above, there is no separate
// `all`/`detail` split to make: `websiteId` alone is both the scope and the whole key.
const schedules = {
  detail: (websiteId: string) => ["schedules", "detail", websiteId] as const,
};

const health = {
  status: ["health"] as const,
};

export const queryKeys = {
  websites,
  runs,
  schedules,
  health,
};
