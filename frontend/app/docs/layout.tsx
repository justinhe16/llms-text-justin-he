import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

export const metadata: Metadata = {
  title: "Docs · llms-text",
  description: "What llms.txt is, how to add a site, and the limits that apply.",
};

/**
 * The frame around `/docs`. One back link, one column, nothing else — no sidebar, no
 * search, no version switcher. The page is one MDX file and the chrome should not outweigh
 * it.
 *
 * `max-w-2xl` is a reading measure, not a layout guess: it puts a line of this page's body
 * text at roughly 70 characters.
 *
 * Public. `/docs` is not in `frontend/middleware.ts`'s `PROTECTED_PREFIXES`, so this renders
 * for a signed-out visitor exactly as it does for a signed-in one — documentation nobody can
 * read before signing up is documentation that cannot do its job.
 */
export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-14 sm:py-20">
      <Link
        href="/"
        className="inline-flex items-center gap-1.5 rounded-md text-sm text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <ArrowLeft className="size-3.5" aria-hidden="true" />
        Back
      </Link>

      <div className="mt-10 pb-10">{children}</div>
    </main>
  );
}
