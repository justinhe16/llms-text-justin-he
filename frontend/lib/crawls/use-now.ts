"use client";

// One clock for the whole page.
//
// A relative timestamp rendered once is wrong a minute later — a row that reads "2m ago"
// for twenty minutes looks like the table has stopped working, which is the exact failure
// this hook exists to prevent. The obvious fix, a `setInterval` inside every
// `<RelativeTime>`, gives a table of thirty rows thirty timers that all fire at different
// moments, so the column updates in a ragged wave rather than at once.
//
// Instead there is a single module-level interval, started when the first component
// subscribes and cleared when the last one unsubscribes, feeding every subscriber the same
// `now`. Thirty rows cost one timer, they all re-render on the same tick, and a page with
// no timestamps on it (or a tab that unmounted the table) runs no timer at all.

import { useSyncExternalStore } from "react";

/** How often the shared clock advances. Ten seconds, not one: nothing this column renders
 * changes faster than once a minute past the first "now" (see `NOW_THRESHOLD_MS` in
 * relative-time.ts), so a one-second tick would re-render every row sixty times to change
 * one character. Ten seconds bounds the visible staleness of an "Nm ago" at ten seconds,
 * which is well under the granularity anyone can perceive in this column. */
const TICK_MS = 10_000;

// Captured at module evaluation, and deliberately the value `getServerSnapshot` returns
// too. `useSyncExternalStore` requires a server snapshot, and it is also what React reads
// during hydration — so returning a *fresh* `Date.now()` there would hand the server and
// the client two different numbers for the same render pass.
//
// In practice nothing subscribed to this clock renders during hydration at all: every
// consumer sits inside the /crawls table, which has no server-fetched data (React Query
// fetches in the browser) and therefore renders its skeleton on both the server pass and
// the hydration pass, with not one timestamp in it. A single constant here keeps that true
// by construction rather than by luck, for whatever mounts this hook next.
const INITIAL_NOW = Date.now();

let currentNow = INITIAL_NOW;
let intervalId: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);

  if (intervalId === null) {
    // The first subscriber also resyncs the clock: `INITIAL_NOW` is as old as this module,
    // which for a page left open on another route could be many minutes stale by the time
    // the table first mounts.
    currentNow = Date.now();
    intervalId = setInterval(() => {
      currentNow = Date.now();
      for (const notify of listeners) notify();
    }, TICK_MS);
  }

  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
  };
}

// Must return a cached value, never a fresh `Date.now()`: `useSyncExternalStore` calls
// `getSnapshot` during render and compares the result with the previous one by `Object.is`
// to decide whether to re-render. A function that returns a new number every call would
// never compare equal, and React would loop.
function getSnapshot(): number {
  return currentNow;
}

function getServerSnapshot(): number {
  return INITIAL_NOW;
}

/** The current time in milliseconds, re-rendering the calling component every
 * `TICK_MS`. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
