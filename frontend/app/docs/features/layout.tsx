import type { Metadata } from "next";

import { FeaturesContents } from "@/components/docs/features-contents";

export const metadata: Metadata = {
  title: "Features · llms-text",
  description:
    "The website/run/schedule model, adding a site, schedules, Trends, the full set of limits, and the API.",
};

/**
 * The "Features" tab's own two-column grid: an in-page contents list, and the reference
 * text. Mirrors `app/docs/(run)/layout.tsx` exactly — same `15rem` left column, same
 * `max-w-2xl` reading measure — because `FeaturesContents` is a list of short links, the
 * same shape the pipeline's node column is, not the topology's stacked, wider one
 * (`app/docs/architecture/layout.tsx`'s `18rem`).
 *
 * See `app/docs/(run)/layout.tsx`'s docstring for why the page scrolls and the column does
 * not — nothing here differs from that reasoning, so it is not restated.
 */
export default function DocsFeaturesLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-10 grid gap-12 pb-10 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start lg:gap-14">
      <FeaturesContents />
      <div className="min-w-0 max-w-2xl">{children}</div>
    </div>
  );
}
