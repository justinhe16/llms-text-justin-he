// Which run the Output tab shows.
//
// Extracted from the component (ARCHITECTURE.md §8.4: a pure derivation belongs in
// `lib/<feature>/`, where it is readable without knowing what the screen looks like) because
// the first version of this lived inline as a `??` chain and was wrong in a way that was very
// hard to see there — see the note below.

import type { RunListItem } from "@/lib/api/runs";

/**
 * The id of the run to display, or `null` when there is nothing to show.
 *
 * The rule, in priority order:
 *
 * 1. **An explicit `?run=` always wins** — unconditionally, and *without* checking whether
 *    that run is among `runs`. `GET /runs/{id}` needs only the id, so a run beyond the
 *    loaded pages is perfectly displayable, and a run id that names nothing produces a `404`
 *    the Output tab renders as an error. Both are better than silently showing a different
 *    run than the URL asked for.
 * 2. The most recent **completed** run. The newest run is often `pending` — someone just
 *    pressed "Run now" and landed here — and defaulting to it would replace an artifact they
 *    can read with a spinner they cannot.
 * 3. The most recent run of any status, so a website whose only run failed shows that
 *    failure rather than an empty page.
 *
 * `runs` arrives newest-first from the API (keyset pagination on `started_at desc`), so the
 * first match in each pass is the most recent one. Nothing here sorts.
 *
 * ## The bug this function exists to have fixed
 *
 * The original inline version was `runs.find(id match) ?? runs.find(completed) ?? runs[0]`,
 * which reads as if it honours `?run=` first. It does not: when the id is not in the loaded
 * page, `find` returns `undefined` and the chain falls through to *a different run*, with no
 * error and nothing on screen saying so. That silently broke both of the things `?run=`
 * exists for — a linkable deep link to an older run, and the `409` recovery in
 * `use-trigger-run.ts` navigating to the run that is already in flight (which, if it was
 * started in another tab or by the scheduler, is exactly the case where it is *not* in this
 * tab's cache). Rule 1 above is deliberately a plain early return rather than another link in
 * a `??` chain, so it cannot silently degrade the same way again.
 */
export function selectRunToShow(
  runs: RunListItem[],
  selectedRunId: string | null
): string | null {
  if (selectedRunId !== null) return selectedRunId;

  const newestCompleted = runs.find((run) => run.status === "completed");
  return newestCompleted?.id ?? runs[0]?.id ?? null;
}
