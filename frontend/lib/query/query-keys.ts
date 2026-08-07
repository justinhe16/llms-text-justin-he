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

import type { RunListOptions, StatsWindow } from "@/lib/api/runs";

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

  // A *prefix* of both `list` and `infinite` below, and the only key here that is not
  // fetched with — it exists purely to be invalidated. React Query matches queries by key
  // prefix, so `invalidateQueries({ queryKey: queryKeys.runs.forWebsite(id) })` catches
  // every cached page and every filter variant of one website's history in a single call,
  // without touching another website's or any run's detail entry.
  //
  // The alternative, `runs.all`, is the reason this exists: it is correct but far too
  // coarse for the one caller that needs this (lib/query/use-trigger-run.ts). Triggering a
  // run on website A has no bearing whatsoever on website B's history, and invalidating
  // `["runs"]` would refetch every run list *and* every `runs.detail` entry currently
  // mounted — including the multi-thousand-line `llms_txt` the Output tab is displaying,
  // for a run that certainly did not change.
  forWebsite: (websiteId: string) => ["runs", "list", websiteId] as const,

  // The same list as `list` above, read through `useInfiniteQuery` instead of `useQuery`
  // (lib/query/use-runs-infinite.ts). It gets its own key rather than sharing `list`'s
  // because the two cache genuinely different shapes under React Query: an infinite query
  // stores `{ pages, pageParams }`, a plain one stores a bare `RunPage`, and a component
  // reading one from a cache entry written by the other would crash rather than merely
  // render something stale.
  //
  // `filters` is `RunListOptions` minus `cursor` — deliberately, and this is the whole
  // point of the type below. In an infinite query the cursor is the *page parameter*, not
  // part of the key: baking it in would give every "Load more" click its own cache entry
  // again, which is exactly the accumulation problem `useInfiniteQuery` exists to solve.
  // Spelled as an `Omit` of the real options type rather than a hand-written `{ status?,
  // limit? }` so a fourth query parameter added to `RunListOptions` cannot quietly go
  // missing from this key.
  infinite: (websiteId: string, filters?: Omit<RunListOptions, "cursor">) =>
    ["runs", "list", websiteId, "infinite", filters] as const,

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

// `GET /websites/{id}/stats` is run *statistics*, and `lib/api/runs.ts` owns its helper for
// that reason — but its cache entries are deliberately NOT nested under `runs` above.
//
// `runs.forWebsite(id)` exists to be invalidated, and it is a prefix of `["runs", "list",
// websiteId]`. Filing stats under the same root would either sit outside that prefix (making
// the nesting decorative) or inside it (making every "this website's history changed"
// invalidation also drop an aggregate the Trends tab may be rendering). A separate root says
// what is true: these are two resources that happen to be about the same underlying rows.
//
// `window` is part of the key for the same reason `include` is part of `websites.list` —
// `?window=1d` and `?window=14d` are different responses with different `bucket` fields and
// different series lengths, and one key for both would let a switch to 14d render 1d's
// hourly buckets under day labels until the refetch landed.
const stats = {
  detail: (websiteId: string, window: StatsWindow) =>
    ["stats", "detail", websiteId, window] as const,
};

const health = {
  status: ["health"] as const,
};

export const queryKeys = {
  websites,
  runs,
  schedules,
  stats,
  health,
};
