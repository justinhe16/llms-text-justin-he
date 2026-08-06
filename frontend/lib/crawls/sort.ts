// Client-side sorting for the /crawls table.
//
// Client-side on purpose, and stated as a decision rather than left as an accident: `GET
// /websites` takes no `sort` parameter and returns every website in one unpaginated
// response, so the whole list is already in memory the moment the table renders. Sorting it
// here costs one pass over an array; sorting it on the server would cost a round trip, a new
// query parameter in the cache key, and a spinner between clicking a column and seeing it
// reorder. If this list ever grows past the point where that is true, the fix is pagination
// on the endpoint, and this module is what gets deleted.

import type { WebsiteListItem } from "@/lib/api/websites";

import { lastActivityAt, rowStatus, rowStatusSortWeight } from "./row-status";

/** The three sortable columns. Pages, Schedule and Owner are deliberately not sortable:
 * none of them is something a reader scans this table looking for, and every extra
 * clickable header is one more thing between them and the row they came for. */
export type SortKey = "site" | "lastRun" | "status";

export type SortDirection = "asc" | "desc";

export interface SortState {
  key: SortKey;
  direction: SortDirection;
}

/**
 * Most recent activity first. The table's job is to answer "what has been happening", and
 * the answer is at the top of this ordering.
 */
export const DEFAULT_SORT: SortState = { key: "lastRun", direction: "desc" };

/**
 * Which direction a column starts in when you first click it. Text sorts A→Z, because that
 * is what every table everywhere does. The other two start at the end that is worth looking
 * at: the newest run, and the most alarming status.
 */
const INITIAL_DIRECTION_BY_KEY: Record<SortKey, SortDirection> = {
  site: "asc",
  lastRun: "desc",
  status: "asc",
};

/** Clicking a column: the same column flips direction, a different one switches to it in
 * that column's own natural starting direction (rather than inheriting whatever direction
 * the previous column happened to be in, which produces a reverse-alphabetical site list
 * for no reason the reader can see). */
export function nextSortState(current: SortState, key: SortKey): SortState {
  if (current.key === key) {
    return { key, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { key, direction: INITIAL_DIRECTION_BY_KEY[key] };
}

/** `aria-sort` for a `<th>`: the value for the active column, `"none"` for the rest. This
 * is the only thing that tells a screen reader the table is sorted at all — the arrow glyph
 * beside the label is invisible to one. */
export function ariaSortFor(state: SortState, key: SortKey): "ascending" | "descending" | "none" {
  if (state.key !== key) return "none";
  return state.direction === "asc" ? "ascending" : "descending";
}

function compareOrigin(a: WebsiteListItem, b: WebsiteListItem): number {
  return a.origin.localeCompare(b.origin);
}

/**
 * Newest first when `sign` is negative. Websites with no run at all sort to the bottom in
 * **both** directions rather than flipping to the top on a reversed sort: "oldest last run
 * first" and "sites that have never run" are different questions, and answering the second
 * one at the top of the first one's results just buries the row the reader asked for.
 */
function compareLastRun(a: WebsiteListItem, b: WebsiteListItem, sign: number): number {
  const aAt = lastActivityAt(a);
  const bAt = lastActivityAt(b);
  if (aAt === null && bAt === null) return 0;
  if (aAt === null) return 1;
  if (bAt === null) return -1;
  return sign * (Date.parse(aAt) - Date.parse(bAt));
}

function compareStatus(a: WebsiteListItem, b: WebsiteListItem): number {
  return rowStatusSortWeight(rowStatus(a)) - rowStatusSortWeight(rowStatus(b));
}

/**
 * A new array, sorted. Never mutates its argument — the array it is handed is React Query's
 * cached data, and sorting that in place would reorder the cache itself, so a component that
 * read the same query without sorting would silently see this table's ordering.
 *
 * Every comparator falls back to origin so the result is **total**: two rows that tie on the
 * sorted column always land in the same order relative to each other, on every render and
 * every poll tick. Without that, rows with equal timestamps could swap places each time the
 * 3-second poll returns, which reads as the table flickering.
 */
export function sortWebsites(websites: WebsiteListItem[], state: SortState): WebsiteListItem[] {
  const sign = state.direction === "asc" ? 1 : -1;

  return [...websites].sort((a, b) => {
    let primary = 0;
    switch (state.key) {
      case "site":
        primary = sign * compareOrigin(a, b);
        break;
      case "lastRun":
        primary = compareLastRun(a, b, sign);
        break;
      case "status":
        primary = sign * compareStatus(a, b);
        break;
    }
    return primary !== 0 ? primary : compareOrigin(a, b);
  });
}
