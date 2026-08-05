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

// No `runs` key here yet. `GET /websites/{id}/runs` does not exist (see the note at the
// top of lib/api/websites.ts), so there is nothing to key a run list or a single run by.
// It slots in beside `websites` above, as `runs: { all, list: (websiteId) => ..., detail:
// (id) => ... }`, when that endpoint's ticket lands.

const health = {
  status: ["health"] as const,
};

export const queryKeys = {
  websites,
  health,
};
