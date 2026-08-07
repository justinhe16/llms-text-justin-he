// Scrolling the document to a `/docs` section, and the reduced-motion decision that comes
// with it.
//
// This is a function rather than a hook because it only ever runs from a click handler,
// where `window` is guaranteed. Reading `prefers-reduced-motion` at call time — rather than
// subscribing to it in a hook and holding it in state — means the answer is never stale and
// nothing has to render twice on the server to find it out.

/**
 * Scroll the document to the heading with `id`.
 *
 * The heading is scrolled to *in the document*, not inside a scroll container: the right
 * column of `/docs` deliberately has no `overflow` of its own, so this moves the page. That
 * is what keeps the URL bar, the browser's back/forward scroll restoration, and
 * `scroll-margin-top` (set on headings in `mdx-components.tsx`) all working.
 *
 * Under `prefers-reduced-motion: reduce` the jump is instant. Note that this query is
 * allowed here — ARCHITECTURE.md §8.5 bans `prefers-color-scheme`, which is a different
 * media feature and a different rule.
 *
 * @param id The heading id, from `DOCS_SECTIONS` in `./sections`.
 * @returns `true` when a heading with that id existed and was scrolled to.
 */
export function scrollToSection(id: string): boolean {
  const target = document.getElementById(id);
  if (!target) return false;

  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;

  target.scrollIntoView({
    behavior: prefersReducedMotion ? "auto" : "smooth",
    block: "start",
  });
  return true;
}
