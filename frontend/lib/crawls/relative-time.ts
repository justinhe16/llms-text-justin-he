// Timestamp formatting for the /crawls table. Pure functions over an ISO string and an
// explicit `now` — no `Date.now()` is read in here, which is what makes these callable from
// a render pass without the result depending on when that pass happened to run. The clock
// itself lives in `use-now.ts`, and the component wiring in
// components/crawls/relative-time.tsx.

/** Below this, a timestamp reads "now" rather than a count of seconds. A run that started
 * eleven seconds ago and one that started thirty do not differ in any way a reader can act
 * on, and a seconds counter in a table that repaints every three seconds is noise. */
const NOW_THRESHOLD_MS = 45_000;

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const WEEK_MS = 7 * DAY_MS;
const MONTH_MS = 30 * DAY_MS;
const YEAR_MS = 365 * DAY_MS;

/**
 * "now" / "2m ago" / "6h ago" / "3d ago" / "2w ago" / "5mo ago" / "1y ago".
 *
 * Compact rather than `Intl.RelativeTimeFormat`'s "2 minutes ago" on purpose: this is a
 * dense table column that must not wrap, and the units are unambiguous at a glance in that
 * context. The trade-off is that this is English-only — the app has no i18n and no ticket
 * that adds one; when it does, this is the single function that changes.
 *
 * A timestamp in the future (clock skew between the browser and the database, most likely)
 * clamps to "now" rather than rendering "-3m ago", which would read as a bug.
 */
export function formatRelativeTime(iso: string, now: number): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";

  const elapsed = now - then;
  if (elapsed < NOW_THRESHOLD_MS) return "now";
  if (elapsed < HOUR_MS) return `${Math.floor(elapsed / MINUTE_MS)}m ago`;
  if (elapsed < DAY_MS) return `${Math.floor(elapsed / HOUR_MS)}h ago`;
  if (elapsed < WEEK_MS) return `${Math.floor(elapsed / DAY_MS)}d ago`;
  if (elapsed < MONTH_MS) return `${Math.floor(elapsed / WEEK_MS)}w ago`;
  if (elapsed < YEAR_MS) return `${Math.floor(elapsed / MONTH_MS)}mo ago`;
  return `${Math.floor(elapsed / YEAR_MS)}y ago`;
}

/**
 * "due now" / "in 12m" / "in 4h 12m" / "in 6d 3h" — the future-facing counterpart to
 * `formatRelativeTime` above, and the reason that function could not simply be reused for a
 * schedule's `next_run_at`: it clamps *every* future timestamp to "now" on purpose, so a run
 * due in four hours would render as "now" rather than as a countdown.
 *
 * Two units, never three. "in 6d 3h 41m" is a number nobody reads to the end, and the third
 * unit is always the one that changes fastest — so it would also be the one making the line
 * repaint for no benefit. The larger unit alone ("in 6d") loses the distinction between a run
 * due tonight and one due tomorrow morning, which is exactly what somebody checking this
 * panel wants to know.
 *
 * A timestamp already in the past reads "due now" rather than counting upward. That state is
 * real — the cron tick fires on an interval, so a schedule can genuinely sit a little past due
 * — and "due now" describes it correctly, where "3m ago" would suggest the run had happened.
 */
export function formatCountdown(iso: string, now: number): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";

  const remaining = then - now;

  // Symmetric with `NOW_THRESHOLD_MS` on the past side: under a minute either way is "now",
  // and the shared clock only ticks every ten seconds regardless (see use-now.ts).
  if (remaining < NOW_THRESHOLD_MS) return "due now";

  if (remaining < HOUR_MS) return `in ${Math.floor(remaining / MINUTE_MS)}m`;

  if (remaining < DAY_MS) {
    const hours = Math.floor(remaining / HOUR_MS);
    const minutes = Math.floor((remaining % HOUR_MS) / MINUTE_MS);
    return minutes === 0 ? `in ${hours}h` : `in ${hours}h ${minutes}m`;
  }

  const days = Math.floor(remaining / DAY_MS);
  const hours = Math.floor((remaining % DAY_MS) / HOUR_MS);
  return hours === 0 ? `in ${days}d` : `in ${days}d ${hours}h`;
}

// Built once rather than per call: constructing an Intl formatter is the expensive part,
// and a table of rows re-rendering on every poll tick would otherwise build one per cell
// per tick. `undefined` for the locale means "whatever the browser is set to", which is
// correct for an absolute timestamp shown to the person reading it — and is also why
// everything in this file runs in the browser only (see components/crawls/relative-time.tsx
// for how that is guaranteed).
const absoluteFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

/** The full, unambiguous timestamp — what the tooltip shows, and what `formatRelativeTime`
 * above is a lossy summary of. */
export function formatAbsoluteTime(iso: string): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  return absoluteFormatter.format(parsed);
}
