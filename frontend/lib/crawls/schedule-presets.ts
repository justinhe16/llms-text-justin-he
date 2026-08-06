// The four interval presets, as the Schedule tab offers them.
//
// THE ONE RULE THIS FILE EXISTS TO ENFORCE: the set of presets is never hand-written here.
// `ScheduleIntervalMinutes` (lib/api/schedules.ts) is an indexed access into the generated
// OpenAPI types, which trace back to `ScheduleIntervalMinutes` in
// backend/app/features/schedules/schemas.py — a pydantic `Literal`, chosen partly so the
// allowed values reach this client instead of a bare `integer`. `PRESET_LABELS` below is an
// exhaustive `Record` over that union, so the compiler, not a reviewer, is what notices when
// the backend adds a fifth preset: the object literal fails to type-check until a label for
// it is written. A `const PRESETS: ScheduleIntervalMinutes[] = [60, 360, 1440, 10080]` would
// compile forever and quietly go on offering four options.
//
// This is deliberately NOT `formatIntervalMinutes` (lib/crawls/row-status.ts). That function
// renders any integer in the compact register a six-column table needs ("daily", "6h") and
// has to tolerate values outside these four; these are option labels in a picker, written the
// way a person reads a sentence ("Every 6 hours"). Two registers of the same fact, not two
// copies of it — and only this one is allowed to be exhaustive, because only this one is a
// closed set.

import type { ScheduleIntervalMinutes } from "@/lib/api/schedules";

export interface ScheduleIntervalOption {
  minutes: ScheduleIntervalMinutes;
  label: string;
}

const PRESET_LABELS: Record<ScheduleIntervalMinutes, string> = {
  60: "Hourly",
  360: "Every 6 hours",
  1440: "Daily",
  10080: "Weekly",
};

/**
 * The presets in ascending order, which is also the order the radio group renders them in.
 *
 * The `as ScheduleIntervalMinutes` is the one assertion in this file and it is load-bearing
 * only in a narrow way: `Object.entries` types its keys as `string` regardless of how the
 * `Record` was declared, so the numeric key that provably came from `PRESET_LABELS` has to be
 * named again. The `.sort` is not decoration — JavaScript happens to enumerate integer-like
 * keys in ascending numeric order, and relying on that accident to order a UI is exactly the
 * kind of thing that breaks when someone renames a key to a string.
 */
export const SCHEDULE_INTERVAL_OPTIONS: readonly ScheduleIntervalOption[] = Object.entries(
  PRESET_LABELS
)
  .map(([minutes, label]) => ({ minutes: Number(minutes) as ScheduleIntervalMinutes, label }))
  .sort((first, second) => first.minutes - second.minutes);

/**
 * What a website with no schedule shows before anything is chosen.
 *
 * Daily rather than hourly: it is the interval that suits most sites, and an accidental
 * enable costs one crawl a day rather than twenty-four. The ticket's reasoning for
 * preselecting anything at all is the part worth keeping in mind — a toggle that does nothing
 * until you *also* pick an interval reads as a bug, so enabling has to be one action.
 */
export const DEFAULT_SCHEDULE_INTERVAL_MINUTES: ScheduleIntervalMinutes = 1440;

/**
 * Whether a stored `interval_minutes` is one of the presets this UI can render.
 *
 * This is not paranoia about a value the app itself could produce — `PUT` only accepts the
 * four. `ScheduleResponse.interval_minutes` is a plain `int` on purpose (the backend's own
 * comment explains that re-validating the allowlist on the way out would turn a hand-seeded
 * or legacy row into a `500` on a plain `GET`), so the one honest thing a picker can do with
 * a value outside its options is decline to claim one of them is selected.
 */
export function isScheduleIntervalMinutes(minutes: number): minutes is ScheduleIntervalMinutes {
  return SCHEDULE_INTERVAL_OPTIONS.some((option) => option.minutes === minutes);
}

/**
 * The spread PER-164 applies to `next_run_at`, as a fraction of the interval.
 *
 * Mirrored here only to describe the behaviour in a tooltip — nothing in the frontend applies
 * jitter, and this value must not be used to compute or correct a displayed time. The server
 * sends the already-jittered `next_run_at`; this constant exists so the panel can explain why
 * a daily run lands at 02:11 instead of 02:00 before somebody files that as a bug.
 */
export const SCHEDULE_JITTER_FRACTION = 0.05;

/** "3m" / "18m" / "1h 12m" / "8h 24m" — a plain duration, for the jitter sentence. */
function formatMinuteSpan(totalMinutes: number): string {
  const rounded = Math.max(1, Math.round(totalMinutes));
  if (rounded < 60) return `${rounded}m`;
  const hours = Math.floor(rounded / 60);
  const minutes = rounded % 60;
  return minutes === 0 ? `${hours}h` : `${hours}h ${minutes}m`;
}

/** How far either side of the displayed time a run may actually fire, e.g. `"1h 12m"` for a
 * daily schedule. */
export function jitterWindow(intervalMinutes: number): string {
  return formatMinuteSpan(intervalMinutes * SCHEDULE_JITTER_FRACTION);
}
