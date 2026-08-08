import type { Metadata } from "next";

import { DocsDiagram } from "@/components/docs/docs-diagram";

export const metadata: Metadata = {
  title: "Docs · llms-text",
  description: "What llms.txt is, and the seven stages a run passes through.",
};

/**
 * The "How a run works" tab's own two-column grid: the pipeline diagram, and the reference
 * text. `app/docs/layout.tsx` supplies the shared frame — the back link and the tab nav —
 * and this route group owns only what is specific to this tab, starting with `metadata`.
 *
 * ## The grid
 *
 * Two columns from `lg` up; one column below it, diagram first. The diagram is the same
 * component either way — its beams measure from the DOM and its nodes still scroll the page,
 * so stacking it costs nothing and needs no second code path.
 *
 * `max-w-2xl` on the text column is a reading measure, not a layout guess: it puts a line of
 * this page's body text at roughly 70 characters. It survived the rebuild unchanged, which is
 * why the outer container grew instead.
 *
 * ## Why the page scrolls and the column does not
 *
 * The left column is `lg:sticky` (see `DocsDiagram`) and the right column is plain flow. No
 * element here owns a scrollbar. That is deliberate: `scrollIntoView` then moves the
 * *document*, so the browser's back/forward scroll restoration keeps working, `scroll-mt-28`
 * on the headings applies, and a link to `/docs#fetch` lands where it should. Giving the
 * right column `overflow-y-auto` would break all three at once.
 *
 * ## Why the bottom padding is this large
 *
 * `Output` is the last of the seven headings, and clicking its diagram node has to bring
 * *that heading's own top* to the top of the viewport (`scrollToSection`'s `block: "start"`).
 * A browser cannot scroll past the end of the document, so if too little page remains below
 * `Output` once it is at the top of the screen, the scroll stops short and the heading — and
 * the node lit for it (`use-active-section.ts`'s own docstring names this exact edge case) —
 * both land wrong. PER-192 moved four sections that used to follow `Output` onto the Features
 * tab, which is what shortened this page enough to trigger it; this padding is the page's
 * standing guarantee of scroll room below the last heading, not a one-off tuned to today's
 * content. `frontend/scripts/smoke.mjs`'s click-to-scroll assertion is what catches a value
 * too small to still cover it.
 */
export default function DocsRunLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-10 grid gap-12 pb-10 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start lg:gap-14">
      <DocsDiagram />
      <div className="min-w-0 max-w-2xl pb-96">{children}</div>
    </div>
  );
}
