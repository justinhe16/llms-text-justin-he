// Presentation helpers for a single *run*, as the detail page's Runs and Output tabs render
// one.
//
// The neighbouring `row-status.ts` covers the same ground for a *website row* in the
// /crawls table — `pagesCrawled(website)` there reads the number off the folded
// `latest_run`, where `runPagesCrawled(run)` here reads it off a run of its own. Two
// functions rather than one because the two responses genuinely differ: `LatestRunSummary`
// carries `pages_crawled` as a typed field the backend already extracted, while
// `RunListItemResponse` carries only the raw `stats` jsonb. Timestamps are not duplicated at
// all — `relative-time.ts` serves both.

import type { RunListItem } from "@/lib/api/runs";

/**
 * The number of pages a run crawled, or `null` when that is genuinely unknown.
 *
 * `runs.stats` is a jsonb column whose shape belongs to the crawler milestone
 * (ARCHITECTURE.md §3.4), so the backend types it `dict[str, Any]` and the generated client
 * types it as an open record. Reading `pages_crawled` out of it is therefore a runtime
 * question, and this is the frontend's only answer to it for a run row.
 *
 * The guard mirrors the SQL the backend already uses for the same field on `GET
 * /websites?include=latest_run` (`jsonb_typeof(latest.stats -> 'pages_crawled') = 'number'`
 * in backend/app/features/websites/internals/websites_reader.py): missing, null, or
 * not-a-number all yield `null` — the same answer as "no stats yet" — instead of `NaN`
 * landing in a table cell. Rendering those cases identically is intentional; there is
 * nothing a reader could do differently about a malformed `stats` than an absent one.
 *
 * `Number.isFinite` rather than `typeof === "number"` because `NaN` and `Infinity` are both
 * numbers to JavaScript and neither is a page count. `0` is a real answer — a run that
 * crawled nothing — and is preserved, matching `pagesCrawled` in row-status.ts.
 */
export function runPagesCrawled(run: Pick<RunListItem, "stats">): number | null {
  const value = run.stats?.pages_crawled;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * A run's elapsed time, from the `duration_ms` the backend computes.
 *
 * `null` is returned as `null`, not "0s", and the caller renders it as an `<EmptyCell>`:
 * `duration_ms` is `null` for exactly one reason — the run has not finished — and "0s" would
 * claim it finished instantly.
 *
 * The two bands exist because the useful precision changes with the magnitude. Under a
 * minute, tenths of a second distinguish one crawl from another; past that they are noise,
 * and zero-padded minutes and seconds line up in a table column the way a decimal never
 * does.
 */
export function formatDuration(durationMs: number | null | undefined): string | null {
  if (durationMs === null || durationMs === undefined || durationMs < 0) return null;

  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(1)}s`;

  const totalSeconds = Math.round(durationMs / 1_000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);

  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}h ${pad(minutes)}m` : `${minutes}m ${pad(seconds)}s`;
}

/**
 * The filename the Output tab's Download button saves under, derived from a website's
 * `origin` — `https://example.com` becomes `llms-example-com.txt`.
 *
 * The ticket words this as `llms-{origin}.txt`, and this is that with the parts of an origin
 * a filename cannot contain removed. `https://example.com:8443` has a scheme separator and a
 * colon in it; `:` is illegal in a filename on Windows and reserved on macOS, and `/` would
 * be read as a path separator everywhere. Substituting rather than stripping keeps the port
 * distinguishable (`example-com-8443`) instead of silently gluing it to the host.
 *
 * This names only the local copy — the file's *contents* are the artifact unchanged — so a
 * user downloading three sites' artifacts gets three distinguishable files instead of
 * `llms.txt`, `llms (1).txt`, `llms (2).txt`.
 *
 * **A second implementation of this function exists in Python**, `artifact_filename` in
 * `backend/app/features/runs/internals/artifact_filename.py` — it names the download
 * `GET /runs/{id}/llms.txt` and `GET /runs/{id}/llms-full.txt` (PER-181) offer, and it must
 * agree with this function character for character or the same website's artifact lands in
 * a user's Downloads folder under two different names depending on which button produced
 * it. There is no seam between the two — this one names a `Blob` the browser already holds
 * and never makes a request — so what keeps them from drifting is
 * `backend/tests/test_artifact_filename.py`'s parity table, checked against both
 * implementations by hand whenever either one changes.
 */
export function llmsTxtFilename(origin: string): string {
  const withoutScheme = origin.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  const safe = withoutScheme
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  return `llms-${safe || "site"}.txt`;
}
