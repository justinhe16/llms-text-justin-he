// Presentation helpers for `GET /websites/{id}/stats`, as the detail page's Trends tab
// renders it. Pure functions over the response — no clock is read in here and no React is
// imported, for the same reason `relative-time.ts` next door avoids both: everything below is
// callable from a render pass and answerable on its own (ARCHITECTURE.md §8.4).
//
// The neighbouring `run-display.ts` formats ONE run for a table cell; this file folds MANY
// runs into the shapes the Trends tab's tiles and charts read.
//
// As of PER-193 the tab reports on the ARTIFACT — index size, pages added/removed/changed —
// rather than on the crawler's own timing, and `msToSeconds`/`avg_duration_ms`/`avg_pages` are
// no longer part of that story. The API keeps both fields (removing a response field is a
// breaking change with no user-visible benefit), but nothing under `components/crawls/`
// reads either any more — `TrendsHealthRow` (trends-stat-tiles.tsx), the health summary that
// replaces the old duration/pages tiles, is one line of `total_runs`/`success_rate` and
// touches neither. Do not reintroduce a duration helper here on the strength of "this file
// used to have one" — the ticket's acceptance criteria are explicit that run duration is
// demoted out of the panel entirely.

import type { StatsBucket, StatsPoint, StatsTotals, WebsiteStats } from "@/lib/api/runs";

// ---------------------------------------------------------------------------------------
// Timestamps
// ---------------------------------------------------------------------------------------
//
// EVERY formatter below is pinned to UTC, and that is a correctness decision rather than a
// stylistic one.
//
// The backend buckets on `date_trunc($5, r.started_at, 'UTC')` (runs_reader.py), so a `day`
// bucket is a UTC day and an `hour` bucket is a UTC hour. Rendering the bucket that starts at
// 2026-08-05T00:00:00Z in, say, America/Los_Angeles would label it "Aug 4, 5:00 PM" — a
// bucket that contains only runs from August 5th, displayed under August 4th. The axis would
// be off by one day for most of the world, and nothing about the chart would look wrong.
//
// So the labels say what the buckets are, and the panel prints "times in UTC" once so the
// reader knows which clock they are reading. This is the one place in the app that departs
// from `relative-time.ts`'s browser-local `formatAbsoluteTime`, which is right for its own
// job: a run's `started_at` is an instant, and an instant is best shown in the reader's own
// time. A bucket is not an instant — it is a labelled UTC interval.
//
// The locale is pinned to "en-US" for the same reason, plus one more: an axis tick's width
// has to be predictable enough to lay out, and "whatever the browser is set to" would render
// a timezone that is not the reader's in a format that is. The app has no i18n; when it gets
// one, these four formatters are what change.

const UTC = "UTC";

/** "Aug 5" — a `day` bucket's axis tick. */
const dayTickFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: UTC,
  month: "short",
  day: "numeric",
});

/**
 * "Thu, 14:00" — an `hour` bucket's axis tick.
 *
 * The weekday is what makes an hourly tick unambiguous without a date. As of PER-193, `hour`
 * buckets serve **both** the 1d and 7d windows (`internals/stats_window.py`) — 24 buckets or
 * 168 — and this formatter cannot tell which window produced a given point, because it formats
 * from `bucket`, not from `window` (see `formatBucketTick`'s docstring for why that is the
 * rule and must stay the rule). For 1d, the weekday prefix is redundant — it repeats the same
 * word across all 24 ticks — but redundant is the safe failure mode: for 7d, a bare "14:00"
 * would appear seven times on one axis meaning seven different afternoons, which is actively
 * misleading rather than merely repetitive. Do not add a `window` parameter to this formatter
 * (or `hourLabelFormatter`) to special-case 1d — that would require threading `window` through
 * every call site for a cosmetic difference on the shortest, least ambiguous window.
 */
const hourTickFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: UTC,
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** "Thu, Aug 5" — a `day` bucket in a tooltip, where there is room for the weekday too. */
const dayLabelFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: UTC,
  weekday: "short",
  month: "short",
  day: "numeric",
});

/** "Aug 5, 14:00" — an `hour` bucket in a tooltip. */
const hourLabelFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: UTC,
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/**
 * The short label under one bucket on the x-axis.
 *
 * `bucket` is the API's own `bucket` field and must be passed through from the response —
 * never re-derived from `window`. The mapping happens to be "7d is hourly, everything else is
 * daily" today (`internals/stats_window.py`), but the server is the only thing that knows
 * which buckets it actually aggregated over, and a second copy of that table here is a second
 * thing that can disagree with the data it is labelling.
 */
export function formatBucketTick(iso: string, bucket: StatsBucket): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "";
  return bucket === "hour"
    ? hourTickFormatter.format(parsed)
    : dayTickFormatter.format(parsed);
}

/** The fuller label a tooltip shows for one bucket. Same rule about `bucket`. */
export function formatBucketLabel(iso: string, bucket: StatsBucket): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "";
  return bucket === "hour"
    ? hourLabelFormatter.format(parsed)
    : dayLabelFormatter.format(parsed);
}

// ---------------------------------------------------------------------------------------
// Scalars
// ---------------------------------------------------------------------------------------

/**
 * `success_rate` as a percentage, or `null` when there is no rate to show.
 *
 * **The `null` is the whole point of this function.** The API returns a fraction in `[0, 1]`
 * and returns `null` — never `0` — when the window contains no runs, precisely so that "no
 * data" and "everything failed" stay distinguishable (`service.py`'s `_to_stats`). A caller
 * must therefore branch on `=== null`, or use `??`, and must never use `||`: `0 || "—"`
 * evaluates to the dash, so a website whose every run failed would render exactly like one
 * that never ran, which is backwards in the most damaging possible direction — it hides a
 * total outage behind an "'nothing to report" glyph.
 *
 * This returns a `number | null` rather than a formatted string so the tile can hand a real
 * number to `NumberTicker`; the `null` branch is the tile's to render, as an `EmptyCell`.
 */
export function successRatePercent(rate: number | null): number | null {
  return rate === null ? null : rate * 100;
}

/**
 * Bytes to kilobytes, for the index-size chart's axis and tooltip. `1024`, not `1000` — this
 * mirrors the backend's own KiB/MiB constants (`internals/llms_txt.py`'s `MAX_PAGE_TEXT_BYTES`
 * and `MAX_FULL_TEXT_BYTES`), so a number on this chart and the byte caps that bound it are
 * expressed in the same unit.
 *
 * Not rounded here, for the same reason the deleted `msToSeconds` was not: the chart wants the
 * unrounded value so its y-axis and its tooltip can each pick their own precision.
 */
export function bytesToKb(bytes: number): number {
  return bytes / 1024;
}

/**
 * "+3" / "-2" / "0" — a delta with an explicit sign, for the change chart's tooltip and the
 * "what changed" panel's secondary metrics strip.
 *
 * `toLocaleString()` already renders a negative number with its own `-`; the only case this
 * adds anything is a positive one, where a bare "3" reads as a count rather than a CHANGE.
 * Zero gets no sign either way — "+0" implies a direction nothing actually moved in.
 */
export function signedCount(n: number): string {
  return n > 0 ? `+${n.toLocaleString()}` : n.toLocaleString();
}

// ---------------------------------------------------------------------------------------
// Series and totals
// ---------------------------------------------------------------------------------------

/** One row of the two time-series charts, pre-formatted so neither chart re-derives it. */
export interface TrendPoint {
  /** The bucket's ISO start, kept for React keys and tooltips. */
  t: string;
  /** The x-axis tick text — already bucket-aware, so the chart passes it straight through. */
  tick: string;
  /** The fuller tooltip heading for this bucket. */
  label: string;
  /** How many runs fell in this bucket. `0` for a zero-filled bucket — see below. */
  runs: number;
  /** How many of this bucket's completed runs recorded a comparison. `0` is real — see
   * `StatsPoint.runs_compared`. */
  runsCompared: number;
  /** `bytesToKb(index_bytes)`, or `null` — never `0` — when no completed run in this bucket
   * recorded an index size. The genuine gap `IndexSizeChart`'s `connectNulls={false}` must
   * break the line across, rather than a quiet-hour zero a line should pass straight through. */
  indexKb: number | null;
  /** `index_pages`, same null-vs-zero rule as `indexKb` — always from the same run. */
  indexPages: number | null;
  /** `pages_added`, unchanged — a real `0` when nothing was added or nothing was compared. */
  added: number;
  /** `pages_removed`, unchanged. */
  removed: number;
  /** `-removed` — the diverging bar's downward series. Recharts stacks `added` and
   * `removedNegative` on the same `stackId`, so a bucket with three added and two removed
   * draws a bar reaching +3 and a bar reaching -2, meeting at the zero line the chart draws
   * with a `ReferenceLine`. */
  removedNegative: number;
}

/**
 * The response's `series` as chart rows.
 *
 * **Zero buckets stay zero — for the counts.** The backend zero-fills every empty bucket with
 * `generate_series`, so `series` always has exactly `bucket_count` entries and a quiet hour
 * arrives as `{ runs: 0, runs_compared: 0, pages_added: 0, pages_removed: 0 }`. Those are
 * measurements, not gaps: nothing ran (or nothing was compared), and "nothing happened" is the
 * most useful thing a trend chart can show. So nothing here drops those rows, converts them to
 * `null`, or smooths over them.
 *
 * **`indexKb`/`indexPages` are the other half of the same discipline, pointed the other
 * way.** They are `null`, never `0`, for a bucket with no completed run that recorded an index
 * size — the backend's own `index_pages`/`index_bytes` fields (`RunStatsPoint`) are `null`
 * under exactly that condition, and this function is a straight pass-through of that
 * distinction, not a place that resolves it. A chart rendering `indexKb` must treat `null` as
 * a genuine gap (`connectNulls={false}`); this is not a contradiction of "zero buckets stay
 * zero" above, it is the other half of the same rule — zero means "measured and nothing
 * happened", `null` means "no measurement exists for this bucket."
 */
export function toTrendPoints(stats: WebsiteStats): TrendPoint[] {
  return stats.series.map((point: StatsPoint) => ({
    t: point.t,
    tick: formatBucketTick(point.t, stats.bucket),
    label: formatBucketLabel(point.t, stats.bucket),
    runs: point.runs,
    runsCompared: point.runs_compared,
    indexKb: point.index_bytes === null ? null : bytesToKb(point.index_bytes),
    indexPages: point.index_pages,
    added: point.pages_added,
    removed: point.pages_removed,
    removedNegative: -point.pages_removed,
  }));
}

/** The three mutually exclusive groups the outcome bar is divided into. */
export interface OutcomeBreakdown {
  completed: number;
  failed: number;
  /** `pending` + `processing` — runs that started inside the window and have not landed. */
  inProgress: number;
  /** `completed + failed + inProgress`, i.e. `totals.total_runs`. */
  total: number;
}

/**
 * `totals` split into the segments of the outcome bar.
 *
 * `completed` and `failed` are the only two the API counts explicitly, but `total_runs`
 * counts all four `run_status` values (lib/api/run-status.ts) — so on any website with a
 * crawl in flight, `completed + failed` is less than `total_runs`. A bar drawn from just the
 * two named counts would silently normalise that difference away and show a full-width bar
 * that accounts for fewer runs than the tile beside it claims. The remainder is therefore
 * derived and rendered as its own segment.
 *
 * `Math.max(0, …)` guards a remainder that should be impossible: it can only go negative if
 * the backend's counts disagree with its own total, and clamping keeps a bar renderable
 * instead of feeding a negative width into Recharts.
 */
export function outcomeBreakdown(totals: StatsTotals): OutcomeBreakdown {
  const inProgress = Math.max(0, totals.total_runs - totals.completed - totals.failed);
  return {
    completed: totals.completed,
    failed: totals.failed,
    inProgress,
    total: totals.total_runs,
  };
}

/**
 * Whether the window contains any runs at all.
 *
 * Read off `total_runs` rather than `success_rate !== null` even though the backend derives
 * one from the other, because that is the field whose name says what is being asked. It is
 * also NOT the same question as "has this website ever run": every field on `totals` is
 * scoped to the window, `last_run_at` included, so a site crawled twice last year reports
 * exactly what a site never crawled at all reports. The Trends tab tells those apart using
 * the detail page's run history, which is unscoped — see `components/crawls/trends-tab.tsx`.
 */
export function windowHasRuns(totals: StatsTotals): boolean {
  return totals.total_runs > 0;
}

/**
 * Whether any completed run in the window recorded a comparison against a previous index —
 * what `ChangeChart` (trends-charts.tsx) reads to decide its empty overlay.
 *
 * Read off `totals.runs_compared`, the summed `RunStatsPoint.runs_compared`, rather than
 * scanning `series` for a nonzero `added`/`removed` — a window where every compared run
 * happened to add and remove exactly zero pages is still a window WITH comparisons, and
 * `windowHasComparisons` must say so.
 */
export function windowHasComparisons(totals: StatsTotals): boolean {
  return totals.runs_compared > 0;
}

/**
 * Whether any bucket in `points` has a known index size — what `IndexSizeChart`
 * (trends-charts.tsx) reads to decide its empty overlay.
 *
 * `!== null`, never a truthiness check: `indexKb` can legitimately be `0` for a website whose
 * `llms.txt` is an empty document, and `0` is exactly as "has a known size" as any other
 * number.
 */
export function windowHasIndexSizes(points: TrendPoint[]): boolean {
  return points.some((point) => point.indexKb !== null);
}
