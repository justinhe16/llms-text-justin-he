import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { DocsDiagram } from "@/components/docs/docs-diagram";

export const metadata: Metadata = {
  title: "Docs · llms-text",
  description: "What llms.txt is, the seven stages of a run, and the limits that apply.",
};

/**
 * The frame around `/docs`: one back link and two columns — the pipeline diagram, and the
 * reference text. Still no sidebar, no search, no version switcher, and still one MDX file.
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
 * Public. `/docs` is not in `frontend/middleware.ts`'s `PROTECTED_PREFIXES`, so this renders
 * for a signed-out visitor exactly as it does for a signed-in one — documentation nobody can
 * read before signing up is documentation that cannot do its job.
 */
export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-6xl px-6 py-14 sm:py-20">
      <Link
        href="/"
        className="inline-flex items-center gap-1.5 rounded-md text-sm text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <ArrowLeft className="size-3.5" aria-hidden="true" />
        Back
      </Link>

      <div className="mt-10 grid gap-12 pb-10 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start lg:gap-14">
        <DocsDiagram />
        <div className="min-w-0 max-w-2xl">{children}</div>
      </div>
    </main>
  );
}
