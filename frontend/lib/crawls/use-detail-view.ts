"use client";

import { useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

/**
 * The four tabs of `/crawls/[websiteId]`, in the order they are rendered.
 *
 * Two of them — `schedule` and `trends` — are placeholder panels until PER-168 and PER-169
 * land. They are listed here, and rendered, rather than hidden: the shell is supposed to
 * show the page's final shape, and a tab that appears later moves everything sideways at
 * the moment a user has just learned where things are. Replacing a placeholder means
 * swapping one component in `components/crawls/website-detail.tsx` and deleting nothing
 * from this file.
 */
export const DETAIL_TABS = ["runs", "output", "schedule", "trends"] as const;

export type DetailTab = (typeof DETAIL_TABS)[number];

/** The tab a URL with no (or an unrecognized) `?tab=` resolves to. */
export const DEFAULT_DETAIL_TAB: DetailTab = "runs";

// A `readonly [...]` tuple's `.includes` only accepts its own member type, so the argument
// is widened to `string` for the check. The predicate signature is what narrows afterwards,
// and it is doing real work: `?tab=` is user-supplied text, and `<Tabs value="../../etc">`
// would otherwise put an arbitrary string into the tab state.
function isDetailTab(value: string | null): value is DetailTab {
  return value !== null && (DETAIL_TABS as readonly string[]).includes(value);
}

/**
 * What the detail page is currently showing, and how to change it — both stored in the
 * query string so a view can be linked and survives a refresh.
 *
 * ## Two parameters, not one
 *
 * `?tab=` is the ticket's requirement. `?run=` is the Output tab's selected run, and it is
 * a URL parameter for the same reasons `?tab=` is, plus one the ticket forces: clicking a
 * row in the Runs tab has to switch tabs *and* select a run in a single navigation, and the
 * `409` from "Run now" has to select the run that is already in flight. Both are naturally
 * "put the page in this state", which is what a URL is. As component state they would also
 * be lost on the refresh the acceptance criteria ask about.
 *
 * `?run=` is deliberately not validated here beyond being a string. A run id is a UUID the
 * server owns; a made-up one produces a `404` from `GET /runs/{id}` that the Output tab
 * renders as an error, which is a better outcome than this hook silently discarding a
 * parameter and showing a different run than the URL asked for.
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

  // One writer for both parameters, so a change to either is a single `replace` — the row
  // click sets both at once, and two sequential navigations would briefly render the Output
  // tab with the *previous* run selected before correcting itself.
  //
  // `undefined` means "leave this parameter as it is" and `null` means "remove it", which
  // is what lets `setTab` below change tabs without disturbing the selected run.
  const updateView = useCallback(
    (next: { tab?: DetailTab; runId?: string | null }) => {
      const params = new URLSearchParams(searchParams.toString());

      if (next.tab !== undefined) params.set("tab", next.tab);
      if (next.runId !== undefined) {
        if (next.runId === null) params.delete("run");
        else params.set("run", next.runId);
      }

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

  return { tab, selectedRunId, setTab, showRunOutput };
}
