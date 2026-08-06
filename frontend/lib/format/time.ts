// Timestamp and duration formatting for the run history. Pure functions over an ISO string
// or a millisecond count — no React, no hooks, no `Date.now()` captured at module scope, so
// every one of them is deterministic given its arguments and testable without a clock.
//
// `now` is a parameter with a default rather than an unconditional `new Date()` for exactly
// that reason: the default is what every call site uses, and the parameter is what makes
// "what does this render two days later" answerable without mocking global time.
//
// WHY NO LIBRARY. `Intl.RelativeTimeFormat` and `Intl.DateTimeFormat` are both built into
// every browser this app supports and into Node, and between them they cover everything the
// Runs tab needs — localized, correctly pluralized, zero bytes shipped. Adding date-fns or
// dayjs to produce "3 minutes ago" would be a dependency earning its keep on one string.

/**
 * The units `formatRelativeTime` steps through, largest first, each with the number of
 * milliseconds in one of it. Deliberately stops at `day`: months and years are not fixed
 * durations, and a run history that reaches back far enough to need them is better served
 * by the absolute timestamp in the tooltip than by "2 months ago" that is really 67 days.
 */
const RELATIVE_UNITS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
  ["second", 1_000],
];

const relativeFormatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

// `dateStyle`/`timeStyle` rather than hand-picked field options: this string is a tooltip
// whose only job is to be unambiguous, and the locale's own idea of a full date and time is
// better at that than any field list written here would be.
const absoluteFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

/**
 * `iso` as a relative phrase — "3 minutes ago", "yesterday", "in 2 hours".
 *
 * `numeric: "auto"` is what produces "yesterday" instead of "1 day ago"; the sign of the
 * delta is what produces "in 2 hours" for a timestamp in the future, which a `next_run_at`
 * would be even though nothing in this ticket renders one.
 *
 * Anything under a second reads "now" rather than "in 0 seconds" — a run triggered this
 * instant has a `started_at` a few hundred milliseconds in the past or, if the server clock
 * is marginally ahead, in the future, and neither should render as a number.
 */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";

  const deltaMs = then - now.getTime();
  if (Math.abs(deltaMs) < 1_000) return "now";

  for (const [unit, unitMs] of RELATIVE_UNITS) {
    if (Math.abs(deltaMs) >= unitMs) {
      return relativeFormatter.format(Math.round(deltaMs / unitMs), unit);
    }
  }
  return "now";
}

/**
 * `iso` as a full, unambiguous local timestamp — the tooltip behind every relative time in
 * the table, so "3 days ago" is always one hover away from the instant it actually means.
 */
export function formatAbsoluteTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "unknown";
  return absoluteFormatter.format(date);
}

/**
 * A run's elapsed time, from the `duration_ms` the backend computes.
 *
 * `null` renders as an em dash rather than "0s": `duration_ms` is `null` for exactly one
 * reason — the run has not finished — and "0s" would claim it finished instantly.
 *
 * The three bands exist because the useful precision changes with the magnitude. Under a
 * minute, tenths of a second distinguish one crawl from another; past that they are noise,
 * and zero-padded minutes and seconds line up in a table column the way a decimal never
 * does.
 */
export function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "—";
  if (durationMs < 0) return "—";

  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(1)}s`;

  const totalSeconds = Math.round(durationMs / 1_000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);

  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}h ${pad(minutes)}m` : `${minutes}m ${pad(seconds)}s`;
}
