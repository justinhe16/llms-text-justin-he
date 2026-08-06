// Presentation helpers for `GET /websites/{id}/stats`, as the detail page's Trends tab
// renders it. Pure functions over the response — no clock is read in here and no React is
// imported, for the same reason `relative-time.ts` next door avoids both: everything below is
// callable from a render pass and answerable on its own (ARCHITECTURE.md §8.4).
//
// The neighbouring `run-display.ts` formats ONE run for a table cell; this file folds MANY
// runs into the shapes three charts and four tiles read. `formatDuration` there and
// `avgDurationSeconds` here look like duplicates and are not: that one produces a string in
// two bands ("18.2s", "3m 04s") for a column of text, this one produces a bare `number` in
// seconds because a chart axis has to scale it, tick it and interpolate it.

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
 * The weekday is what makes an hourly tick unambiguous without a date: `hour` buckets are
 * only ever used for the 7d window, and no weekday repeats inside seven days. A bare "14:00"
 * would appear seven times on one axis meaning seven different afternoons.
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
 * Milliseconds to seconds, for display. The API reports every duration in milliseconds
 * (`avg_duration_ms`) and every chart and tile in this panel shows seconds, so this is the
 * single place the conversion happens rather than a `/ 1000` scattered across four call
 * sites.
 *
 * Not rounded here. The tile rounds to one decimal through `NumberTicker`'s
 * `decimalPlaces`, and the chart wants the unrounded value so its y-axis can pick its own
 * precision.
 */
export function msToSeconds(ms: number): number {
  return ms / 1000;
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
  /** `avg_pages`, unchanged. */
  pages: number;
  /** `avg_duration_ms` in seconds. */
  seconds: number;
  /** How many runs fell in this bucket. `0` for a zero-filled bucket — see below. */
  runs: number;
}

/**
 * The response's `series` as chart rows.
 *
 * **Zero buckets stay zero.** The backend zero-fills every empty bucket with
 * `generate_series`, so `series` always has exactly `bucket_count` entries and a quiet hour
 * arrives as `{ runs: 0, avg_pages: 0, avg_duration_ms: 0 }`. That is a measurement, not a
 * gap: nothing ran, and "nothing ran" is the most useful thing a trend chart can show. So
 * nothing here drops those rows, converts them to `null`, or smooths over them — and the
 * charts that consume this must not set `connectNulls` or any other gap-bridging option
 * either. A line that glides over a two-day outage is a line that lies about it.
 */
export function toTrendPoints(stats: WebsiteStats): TrendPoint[] {
  return stats.series.map((point: StatsPoint) => ({
    t: point.t,
    tick: formatBucketTick(point.t, stats.bucket),
    label: formatBucketLabel(point.t, stats.bucket),
    pages: point.avg_pages,
    seconds: msToSeconds(point.avg_duration_ms),
    runs: point.runs,
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
