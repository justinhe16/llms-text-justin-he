import type { Metadata } from "next";

import { ArchitectureDiagram } from "@/components/docs/architecture-diagram";

export const metadata: Metadata = {
  title: "Architecture · llms-text",
  description:
    "What is deployed where, how a run gets queued, the concurrency model, and the trade-offs behind each choice.",
};

/**
 * The "Architecture" tab's own two-column grid: the topology diagram, and the reference
 * text. Mirrors `app/docs/(run)/layout.tsx` with one deliberate difference — `18rem` rather
 * than `15rem` for the left column.
 *
 * ## Why 18rem, and why the prose measure is unchanged
 *
 * The topology node is vertically stacked (icon row, then label, then sublabel, all
 * centred) rather than the pipeline node's horizontal `icon + gap-3 + label` row, because at
 * 375px a horizontal row spends 64px on chrome before the first character — see
 * `ArchitectureDiagram`. That stacked shape wants more horizontal room than the pipeline's
 * single-column list to lay two grid columns out side by side without every label wrapping.
 *
 * The extra 3rem does not touch the reading measure: `max-w-6xl` (72rem) − `px-6` (3rem) −
 * `lg:gap-14` (3.5rem) − `18rem` leaves 47.5rem for this column, and its own `max-w-2xl`
 * (42rem) is unchanged — byte-identical to the run tab's text width. The extra room is
 * absorbed by the gap between the (now narrower relative to the page) text column and its
 * `max-w-2xl` cap, not by the prose itself.
 *
 * ## Responsive with no second code path
 *
 * `ArchitectureDiagram`'s own grid is always 2 columns — it does not branch on breakpoint.
 * What changes below `lg` is this *parent*: `lg:grid-cols-[18rem_minmax(0,1fr)]` collapses to
 * one column, so the diagram gets the full content width instead of 18rem. `AnimatedBeam`
 * re-measures on its own (it observes the container and every node — see
 * `components/magicui/animated-beam.tsx`), so that width change re-lays all seven beams with
 * no code here.
 */
export default function DocsArchitectureLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-10 grid gap-12 pb-10 lg:grid-cols-[18rem_minmax(0,1fr)] lg:items-start lg:gap-14">
      <ArchitectureDiagram />
      <div className="min-w-0 max-w-2xl">{children}</div>
    </div>
  );
}
