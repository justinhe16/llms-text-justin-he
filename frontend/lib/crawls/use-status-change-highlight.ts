"use client";

// Which rows just changed status, so the table can say so quietly.
//
// The problem: `useWebsites` polls every three seconds while anything is running
// (lib/query/polling.ts), and a row flipping from `running` to `completed` swaps two words
// and a dot colour somewhere down a list of thirty. On a screen the reader is not staring
// at, that is invisible — which makes the polling feel like it is not working even when it
// is. The fix is a brief background tint on the row that changed, fading out on its own.
//
// The rule this follows, from the ticket: noticeable, not loud. No bouncing, no flashing, no
// entry animation replaying — a background colour that arrives and leaves over the better
// part of a second, and nothing else. `BlurFade` was the other candidate and is the wrong
// tool here: it animates a component *mounting*, and these rows are not mounting, they are
// being updated in place.
//
// This hook only reports *which* rows changed. What that looks like is entirely in
// components/crawls — see `HIGHLIGHT_CLASS` there.

import { useEffect, useRef, useState } from "react";

import type { WebsiteListItem } from "@/lib/api/websites";

import { rowStatus, type RowStatus } from "./row-status";

/** How long a row stays tinted. Long enough to catch the eye of someone whose attention was
 * elsewhere, short enough that a table with several runs finishing in sequence does not end
 * up permanently striped. The CSS transition that fades it back out is shorter than this,
 * so the tint is fully arrived before it starts leaving. */
const HIGHLIGHT_MS = 1_600;

const NO_HIGHLIGHTS: ReadonlySet<string> = new Set();

/**
 * The ids of websites whose row status changed since the previous poll.
 *
 * The first load never highlights anything: every row is "new" then, and tinting the whole
 * table on arrival is exactly the loud thing this is supposed to avoid. Only a row that was
 * present before, with a different status, counts as changed — which also means a website
 * added by someone else appearing mid-poll slides in without a tint, and one that
 * disappears takes its pending timer with it.
 */
export function useStatusChangeHighlight(
  websites: WebsiteListItem[] | undefined
): ReadonlySet<string> {
  const previousStatuses = useRef<Map<string, RowStatus> | null>(null);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const [highlighted, setHighlighted] = useState<ReadonlySet<string>>(NO_HIGHLIGHTS);

  useEffect(() => {
    if (websites === undefined) return;

    const current = new Map(websites.map((website) => [website.id, rowStatus(website)]));
    const previous = previousStatuses.current;
    previousStatuses.current = current;

    // First successful fetch — establish the baseline and tint nothing.
    if (previous === null) return;

    const changed: string[] = [];
    for (const [id, status] of current) {
      const before = previous.get(id);
      if (before !== undefined && before !== status) changed.push(id);
    }
    if (changed.length === 0) return;

    setHighlighted((active) => {
      const next = new Set(active);
      for (const id of changed) next.add(id);
      return next;
    });

    const pending = timers.current;
    for (const id of changed) {
      // A row that changes twice inside the window (pending → processing → completed on a
      // fast run) restarts its timer rather than losing the tint at the first deadline.
      const existing = pending.get(id);
      if (existing !== undefined) clearTimeout(existing);

      pending.set(
        id,
        setTimeout(() => {
          pending.delete(id);
          setHighlighted((active) => {
            if (!active.has(id)) return active;
            const next = new Set(active);
            next.delete(id);
            return next;
          });
        }, HIGHLIGHT_MS)
      );
    }
  }, [websites]);

  // Read the ref into a local before the cleanup closes over it: by the time an unmount
  // cleanup runs, `timers.current` could in principle be a different Map, and eslint's
  // exhaustive-deps rule is right to say so.
  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  return highlighted;
}
