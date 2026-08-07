"use client";

import { useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import type { StatsWindow } from "@/lib/api/runs";

/**
 * The four tabs of `/crawls/[websiteId]`, in the order they are rendered.
 *
 * One of them — `schedule` — is a placeholder panel until PER-168 lands. It is listed here,
 * and rendered, rather than hidden: the shell is supposed to show the page's final shape,
 * and a tab that appears later moves everything sideways at the moment a user has just
 * learned where things are. Replacing a placeholder means swapping one component in
 * `components/crawls/website-detail.tsx` and deleting nothing from this file.
 */
export const DETAIL_TABS = ["runs", "output", "schedule", "trends"] as const;

export type DetailTab = (typeof DETAIL_TABS)[number];

/** The tab a URL with no (or an unrecognized) `?tab=` resolves to. */
export const DEFAULT_DETAIL_TAB: DetailTab = "runs";

/**
 * The three windows the Trends tab offers, in the order the switcher renders them.
 *
 * Typed as `readonly StatsWindow[]` — the union comes from the generated schema
 * (lib/api/runs.ts), so a fourth window added to the backend's `StatsWindowName` literal is a
 * `tsc` error here on the next `npm run gen:api` rather than a switcher quietly missing a
 * button. The order is this app's, not the schema's.
 */
export const STATS_WINDOWS: readonly StatsWindow[] = ["1d", "7d", "14d"];

/** The window a URL with no (or an unrecognized) `?window=` resolves to. Seven days is the
 * ticket's default: one day is too short to show a trend and fourteen is coarser than most
 * readers need for "how is this site's index doing lately". */
export const DEFAULT_STATS_WINDOW: StatsWindow = "7d";

// A `readonly [...]` tuple's `.includes` only accepts its own member type, so the argument
// is widened to `string` for the check. The predicate signature is what narrows afterwards,
// and it is doing real work: `?tab=` is user-supplied text, and `<Tabs value="../../etc">`
// would otherwise put an arbitrary string into the tab state.
function isDetailTab(value: string | null): value is DetailTab {
  return value !== null && (DETAIL_TABS as readonly string[]).includes(value);
}

// The same guard for `?window=`, and it protects something more than tab state does: this
// value is sent to the API as a query parameter and is baked into a React Query cache key.
// An unvalidated `?window=../../x` would be a guaranteed `422` (the backend's
// `StatsWindowName` is a `Literal`) presented to the reader as a broken panel, when falling
// back to the default renders the thing they almost certainly wanted.
//
// This is also what makes a DROPPED window safe rather than a hard failure. `?window=30d` and
// `?window=90d` were live URLs before this ticket shortened `STATS_WINDOWS` to `1d`/`7d`/`14d`,
// and this guard already falls back to `DEFAULT_STATS_WINDOW` for either one — nothing about
// the fallback needed to change when the array did. Resist "simplifying" this into a check
// against a hardcoded list of the old values, or an assumption that every unrecognized string
// is a typo: a bookmarked `30d` link is exactly the case this exists to degrade gracefully
// rather than 422.
function isStatsWindow(value: string | null): value is StatsWindow {
  return value !== null && (STATS_WINDOWS as readonly string[]).includes(value);
}

/**
 * What the detail page is currently showing, and how to change it — both stored in the
 * query string so a view can be linked and survives a refresh.
 *
 * ## Three parameters, not one
 *
 * `?tab=` is the ticket's requirement. `?run=` is the Output tab's selected run, and it is
 * a URL parameter for the same reasons `?tab=` is, plus one the ticket forces: clicking a
 * row in the Runs tab has to switch tabs *and* select a run in a single navigation, and the
 * `409` from "Run now" has to select the run that is already in flight. Both are naturally
 * "put the page in this state", which is what a URL is. As component state they would also
 * be lost on the refresh the acceptance criteria ask about.
 *
 * `?window=` is the Trends tab's time span, and it is here rather than in `useState` inside
 * that panel for exactly the same reason: `?tab=trends&window=14d` has to restore that view
 * on load. It lives in this hook rather than a second `useSearchParams` reader of its own so
 * that all three parameters are written through the one `updateView` below — two independent
 * writers racing on the same query string is how one of them loses a parameter.
 *
 * `?run=` is deliberately not validated here beyond being a string. A run id is a UUID the
 * server owns; a made-up one produces a `404` from `GET /runs/{id}` that the Output tab
 * renders as an error, which is a better outcome than this hook silently discarding a
 * parameter and showing a different run than the URL asked for. `?window=` is the opposite
 * case and IS validated (`isStatsWindow` above): its three values are a closed set this app
 * owns, not a server-side identifier, so an unrecognized one has an obviously correct
 * fallback where a made-up run id does not.
 *
 * ## `replace`, never `push`
 *
 * Every navigation below is `router.replace`. Tabs are a view of one page, not four pages:
 * with `push`, "back" would walk through every tab a user glanced at instead of returning
 * to `/crawls`, and glancing at tabs is exactly what a shell like this invites.
 *
 * `scroll: false` matters just as much and is easier to forget — Next scrolls to the top on
 * navigation by default, so without it, selecting a run from a row halfway down the history
 * would jump the page to the top on the way to the Output tab.
 */
export function useDetailView() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const rawTab = searchParams.get("tab");
  const tab: DetailTab = isDetailTab(rawTab) ? rawTab : DEFAULT_DETAIL_TAB;
  const selectedRunId = searchParams.get("run");

  const rawWindow = searchParams.get("window");
  // Named `statsWindow`, not `window`. The global `window` is in scope in every browser
  // module, and a destructured `const { window } = useDetailView()` at a call site would
  // shadow it — which is legal, silent, and exactly the kind of thing that turns a later
  // `window.matchMedia` in the same component into a runtime crash.
  const statsWindow: StatsWindow = isStatsWindow(rawWindow) ? rawWindow : DEFAULT_STATS_WINDOW;

  // One writer for all three parameters, so a change to any of them is a single `replace` —
  // the row click sets two at once, and two sequential navigations would briefly render the
  // Output tab with the *previous* run selected before correcting itself.
  //
  // `undefined` means "leave this parameter as it is" and `null` means "remove it", which
  // is what lets `setTab` below change tabs without disturbing the selected run.
  const updateView = useCallback(
    (next: { tab?: DetailTab; runId?: string | null; statsWindow?: StatsWindow }) => {
      const params = new URLSearchParams(searchParams.toString());

      if (next.tab !== undefined) params.set("tab", next.tab);
      if (next.runId !== undefined) {
        if (next.runId === null) params.delete("run");
        else params.set("run", next.runId);
      }
      if (next.statsWindow !== undefined) params.set("window", next.statsWindow);

      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams]
  );

  const setTab = useCallback((next: DetailTab) => updateView({ tab: next }), [updateView]);

  /** Select a run *and* switch to the Output tab — the one navigation a row click makes. */
  const showRunOutput = useCallback(
    (runId: string) => updateView({ tab: "output", runId }),
    [updateView]
  );

  /**
   * Change the Trends tab's window.
   *
   * `?window=` is written even when it equals the default, rather than being deleted to keep
   * the URL tidy. Picking "7d" after looking at "14d" is a deliberate choice, and the URL
   * that results has to be copyable as *that* view — a URL that drops the parameter would
   * reopen on whatever the default happens to be at the time, which is the same thing only
   * by coincidence.
   */
  const setStatsWindow = useCallback(
    (next: StatsWindow) => updateView({ statsWindow: next }),
    [updateView]
  );

  return { tab, selectedRunId, statsWindow, setTab, showRunOutput, setStatsWindow };
}
