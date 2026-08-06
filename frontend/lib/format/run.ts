// Presentation helpers for a run that are not timestamps (those are in ./time.ts): reading
// the one field the UI needs out of the untyped `stats` blob, and naming the file the
// Output tab downloads.

import type { RunListItem } from "@/lib/api/runs";

/**
 * The number of pages a run crawled, or `null` when that is genuinely unknown.
 *
 * `runs.stats` is a jsonb column whose shape belongs to the crawler milestone, which is not
 * designed yet (ARCHITECTURE.md §3.4) — so the backend types it `dict[str, Any]` and the
 * generated client types it as an open record. Reading `pages_crawled` out of it is
 * therefore a runtime question, not a compile-time one, and this function is the frontend's
 * only answer to it.
 *
 * The guard deliberately mirrors the SQL the backend already uses for the same field on
 * `GET /websites?include=latest_run` (`jsonb_typeof(latest.stats -> 'pages_crawled') =
 * 'number'` in backend/app/features/websites/internals/websites_reader.py): a value that is
 * missing, null, or not a number yields `null` — the same answer as "no stats yet" —
 * instead of `NaN` or the string "undefined" landing in a table cell. Rendering the two
 * cases identically is intentional; there is nothing a user could do differently about a
 * malformed `stats` than about an absent one.
 *
 * `Number.isFinite` rather than `typeof === "number"` because `NaN` and `Infinity` are both
 * numbers to JavaScript and neither is a page count.
 */
export function runPagesCrawled(run: Pick<RunListItem, "stats">): number | null {
  const value = run.stats?.pages_crawled;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The filename the Output tab's Download button saves under, derived from a website's
 * `origin` — `https://example.com` becomes `llms-example-com.txt`.
 *
 * The ticket words this as `llms-{origin}.txt`, and this is that with the parts of an
 * origin a filename cannot contain removed. `https://example.com:8443` has a scheme
 * separator and a colon in it; `:` is illegal in a filename on Windows and reserved on
 * macOS, and `/` would be read as a path separator everywhere. Substituting rather than
 * stripping keeps the port distinguishable (`example-com-8443`) instead of silently
 * gluing it to the host.
 *
 * The download itself is still `llms.txt`'s *content* — this only names the local copy, so
 * a user downloading artifacts for three sites gets three distinguishable files instead of
 * `llms.txt`, `llms (1).txt`, `llms (2).txt`.
 */
export function llmsTxtFilename(origin: string): string {
  const withoutScheme = origin.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  const safe = withoutScheme
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  return `llms-${safe || "site"}.txt`;
}
