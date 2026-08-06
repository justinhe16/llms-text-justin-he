// What one row of the /crawls table says about a website, derived from the run data
// `GET /websites?include=latest_run` folds into it.
//
// This is the UI's own vocabulary, not the backend's. The API speaks four `runs.status`
// values (`pending`/`processing`/`completed`/`failed` — lib/api/run-status.ts); the table
// has to say something for a fifth situation the API has no word for, because it is the
// absence of a run rather than a run in some state: a website nobody has crawled yet. That
// case renders "Never run" rather than an em dash, because "no value" and "no run has ever
// happened" are different things to a reader and only one of them is worth acting on.
//
// Nothing here decides "is this still in progress" — `isActiveRunStatus`
// (lib/api/run-status.ts) is the only place that question is answered, and this module
// calls it rather than re-deriving it. That matters because the polling predicate
// `useWebsites` runs on is built from the same function: if this file grew its own idea of
// which statuses are terminal, a row could render as finished while the query kept polling
// it, or worse, stop polling a row still rendering as running.

import { isActiveRunStatus, type RunStatus } from "@/lib/api/run-status";
import type { WebsiteListItem } from "@/lib/api/websites";

/**
 * The status vocabulary the table renders. Four of these map one-to-one onto a
 * `RunStatus`; `never-run` is the case where `latest_run` is `null`.
 */
export type RowStatus = "completed" | "running" | "queued" | "failed" | "never-run";

/**
 * `RunStatus` → `RowStatus`, spelled as an exhaustive `Record` for the same reason
 * `ACTIVE_BY_STATUS` in lib/api/run-status.ts is: a fifth value added to the Postgres
 * `run_status` enum reaches `RunStatus` on the next `npm run gen:api`, and this object then
 * fails to compile until someone says what the table should call it. A `switch` with a
 * `default` would silently render the new status as whatever the fallback happened to be.
 *
 * `pending` is "queued" rather than "running" because the two are genuinely different to
 * someone watching the table — a queued run has not started, and a worker that is not
 * draining the queue looks exactly like a stuck "running" if the distinction is erased.
 */
const ROW_STATUS_BY_RUN_STATUS: Record<RunStatus, RowStatus> = {
  pending: "queued",
  processing: "running",
  completed: "completed",
  failed: "failed",
};

/**
 * The four status colour tokens declared in app/globals.css. A literal union rather than
 * `string` so that a call site building `bg-status-${token}` from it cannot name a utility
 * Tailwind never generated — see the comment on `DOT_CLASS` in
 * components/crawls/run-status-indicator.tsx for why that particular mistake is worth
 * making impossible.
 */
export type StatusToken = "completed" | "processing" | "failed" | "idle";

/** Which of the four status colour tokens in app/globals.css a row status paints with.
 *
 * The token names are the contract (`--color-status-completed` and friends); a call site
 * writes `text-status-completed`, never a raw `emerald-*` utility. `queued` and `running`
 * deliberately share the amber "processing" token: they are two phases of the same
 * in-progress state, and giving the queue its own colour would add a fifth hue to a legend
 * that only has four. They are told apart by their label and by the dot's animation, not by
 * colour. */
const TOKEN_BY_ROW_STATUS: Record<RowStatus, StatusToken> = {
  completed: "completed",
  running: "processing",
  queued: "processing",
  failed: "failed",
  "never-run": "idle",
};

const LABEL_BY_ROW_STATUS: Record<RowStatus, string> = {
  completed: "completed",
  running: "running",
  queued: "queued",
  failed: "failed",
  "never-run": "never run",
};

/**
 * Sort weight for the Status column, most-attention-worthy first. A failure is the thing a
 * reader is scanning for, then anything still moving, then the quiet rows. Not alphabetical:
 * "completed / failed / never run / queued / running" is an ordering that tells nobody
 * anything.
 */
const SORT_WEIGHT_BY_ROW_STATUS: Record<RowStatus, number> = {
  failed: 0,
  running: 1,
  queued: 2,
  completed: 3,
  "never-run": 4,
};

/** What the table calls this website's current state. */
export function rowStatus(website: WebsiteListItem): RowStatus {
  const latestRun = website.latest_run;
  return latestRun ? ROW_STATUS_BY_RUN_STATUS[latestRun.status] : "never-run";
}

/**
 * The same mapping as `rowStatus` above, applied to a run's own status rather than to the
 * `latest_run` folded into a website row.
 *
 * The detail page (PER-162) needs this because everything it renders is already a run: the
 * Runs tab's Status column, the run picker in the Output tab, and the status shown beside
 * the origin in the header. All three want the vocabulary and the colours this module owns,
 * and none of them has a `WebsiteListItem` to hand.
 *
 * Exported here rather than re-derived there so `ROW_STATUS_BY_RUN_STATUS` remains the one
 * place a `run_status` becomes a `RowStatus`. There is deliberately no `"never-run"` branch:
 * a run that exists has a status, and "no run has ever happened" is the caller's fact to
 * represent — see `website-detail.tsx`, which falls back to `"never-run"` itself when a
 * website's history is empty.
 */
export function rowStatusFromRunStatus(status: RunStatus): RowStatus {
  return ROW_STATUS_BY_RUN_STATUS[status];
}

/** The status token name — `completed`, `processing`, `failed`, or `idle` — this row's
 * colour comes from. */
export function rowStatusToken(status: RowStatus): StatusToken {
  return TOKEN_BY_ROW_STATUS[status];
}

/** The lowercase label rendered beside the dot. */
export function rowStatusLabel(status: RowStatus): string {
  return LABEL_BY_ROW_STATUS[status];
}

/** Whether this row's run is still moving — the one thing that decides whether the dot
 * animates. Folds through `isActiveRunStatus` rather than testing the `RowStatus` union, so
 * "in progress" still means exactly one thing across the app. */
export function rowIsActive(website: WebsiteListItem): boolean {
  const latestRun = website.latest_run;
  return latestRun ? isActiveRunStatus(latestRun.status) : false;
}

export function rowStatusSortWeight(status: RowStatus): number {
  return SORT_WEIGHT_BY_ROW_STATUS[status];
}

/**
 * The timestamp the "Last run" column shows and the default sort orders on: when the latest
 * run finished, or when it started if it has not finished yet. `null` for a website with no
 * runs at all.
 *
 * Deliberately **not** falling back to `created_at`. Sorting a never-crawled website by the
 * moment someone added it would interleave rows that have no last run at all among rows that
 * do, under a column heading that reads "Last run" — the sort would be doing something other
 * than what the column says. `sortWebsites` instead keeps every `null` at the bottom
 * regardless of direction, and breaks ties among them by origin.
 */
export function lastActivityAt(website: WebsiteListItem): string | null {
  const latestRun = website.latest_run;
  if (!latestRun) return null;
  return latestRun.completed_at ?? latestRun.started_at;
}

/** `pages_crawled` off the latest run — `null` when there is no run, or when the run has
 * one but has not counted any pages yet. `0` is a real answer (a failed run that crawled
 * nothing) and is preserved as `0`, not collapsed into `null`. */
export function pagesCrawled(website: WebsiteListItem): number | null {
  return website.latest_run?.pages_crawled ?? null;
}

/**
 * The schedule interval, rendered the way a person would say it, or `null` when there is no
 * schedule or it is switched off. An inactive schedule reads as no schedule in this column:
 * the row is not going to run on its own either way, and the detail page is where the
 * difference between "none" and "paused" is worth showing.
 */
export function scheduleLabel(website: WebsiteListItem): string | null {
  const schedule = website.schedule;
  if (!schedule || !schedule.active) return null;
  return formatIntervalMinutes(schedule.interval_minutes);
}

/** Minutes → "30m" / "hourly" / "6h" / "daily" / "weekly". The three named cadences are
 * spelled out because they are the ones people actually pick and "1440m" helps nobody. */
export function formatIntervalMinutes(minutes: number): string {
  if (minutes === 60) return "hourly";
  if (minutes === 1440) return "daily";
  if (minutes === 10080) return "weekly";
  if (minutes < 60 || minutes % 60 !== 0) return `${minutes}m`;
  const hours = minutes / 60;
  if (hours % 24 === 0) return `${hours / 24}d`;
  return `${hours}h`;
}
