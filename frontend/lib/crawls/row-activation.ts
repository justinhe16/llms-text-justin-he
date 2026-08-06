"use client";

// Making a whole table row (or a whole card) open a website, without breaking the row.
//
// The naive version — `onClick` on the `<tr>` and nothing else — is mouse-only, and it also
// hijacks every click inside the row, including the ones meant for something else. This
// module is the one place both problems are solved, so the desktop table and the mobile card
// list behave identically rather than each growing its own half of the answer.
//
// A note on what is deliberately absent: `role="link"`. A `<tr>` has an implicit ARIA role
// of `row`, and a table whose rows are not rows is a table a screen reader can no longer
// navigate — you would trade "this row is clickable" for the row/column relationships that
// make the table readable at all. The row stays a row, gains `tabIndex` so it is reachable
// by keyboard, and carries a real `<a>` in its Site cell (see `crawl-row-site-link.tsx`) so
// assistive technology still finds a link, and so ⌘-click and "open in new tab" work the way
// they do everywhere else.

import { useCallback } from "react";
import { useRouter } from "next/navigation";
import type { KeyboardEvent, MouseEvent } from "react";

/** The detail page PER-162 owns. This is the only place the route is spelled, so a change
 * to it is a change to one line. */
export function crawlDetailPath(websiteId: string): string {
  return `/crawls/${websiteId}`;
}

/** Elements inside a row that own their own click. A click landing on one of these must not
 * also navigate — otherwise the site link fires twice (once natively, once through the
 * router), and any future in-row control is unusable. */
const INTERACTIVE_SELECTOR = "a, button, input, select, textarea, [role='button'], [role='menuitem']";

function cameFromInteractiveChild(event: MouseEvent<HTMLElement>): boolean {
  const target = event.target;
  if (!(target instanceof Element)) return false;
  const interactive = target.closest(INTERACTIVE_SELECTOR);
  // `closest` walks up past the row too, so a row that is itself inside some interactive
  // ancestor would match on that ancestor. Only a match strictly *inside* the row counts.
  return interactive !== null && event.currentTarget.contains(interactive);
}

export interface RowActivationProps {
  tabIndex: 0;
  onClick: (event: MouseEvent<HTMLElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLElement>) => void;
}

/** `router.push` to a website's detail page. */
export function useOpenCrawl(): (websiteId: string) => void {
  const router = useRouter();
  return useCallback(
    (websiteId: string) => {
      router.push(crawlDetailPath(websiteId));
    },
    [router]
  );
}

/**
 * The props that make one row activatable: focusable, clickable anywhere that is not
 * already something else, and openable with Enter or Space.
 *
 * Space is handled alongside Enter, and its default prevented, because a focused element
 * that scrolls the page instead of doing the obvious thing is a small, constant annoyance.
 * Both are gated on `event.target === event.currentTarget` so that a keystroke aimed at
 * something inside the row is never stolen by the row.
 */
export function rowActivationProps(
  open: (websiteId: string) => void,
  websiteId: string
): RowActivationProps {
  return {
    tabIndex: 0,
    onClick: (event) => {
      if (cameFromInteractiveChild(event)) return;
      open(websiteId);
    },
    onKeyDown: (event) => {
      if (event.target !== event.currentTarget) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      open(websiteId);
    },
  };
}
