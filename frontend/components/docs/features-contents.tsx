import { FEATURES_SECTIONS } from "@/lib/docs/features";

/**
 * The left column of `/docs/features`: an in-page contents list, not a diagram.
 *
 * There is no graph to draw here — a website, a run, a schedule and a time window are not a
 * pipeline or a topology, and inventing shapes for them would draw a picture this tab does
 * not have. This renders the same left-column slot `DocsDiagram` and `ArchitectureDiagram`
 * fill on the other two tabs, so the three tabs still read as one family, but its content is
 * six plain links to the headings on the right.
 *
 * A server component, deliberately. `DocsDiagram` and `ArchitectureDiagram` are `"use
 * client"` because each drives an `AnimatedBeam` measuring the DOM and (on the pipeline)
 * an `IntersectionObserver` lighting the active node — neither exists here. These are `<a
 * href="#id">` anchors, which the browser already handles without a click listener, so
 * nothing about this component needs to run after hydration.
 *
 * `data-features-contents` / `data-features-link` mirror `data-docs-diagram`/`data-docs-node`
 * and `data-arch-diagram`/`data-arch-node` — the same selector shape `scripts/smoke.mjs`
 * already reads for the other two tabs, applied here to confirm every link still resolves to
 * a real `h2` on the page (ARCHITECTURE.md §8.4's "ids that cross a file boundary need a
 * gate", for a third file).
 */
export function FeaturesContents() {
  return (
    <nav aria-label="On this page" className="lg:sticky lg:top-14">
      {/* A <p>, not a heading — see DocsDiagram's identical choice and its reason: this
          column renders before the article in the DOM, so an <h2> here would start the
          document outline at level 2. The <nav>'s aria-label already names the landmark. */}
      <p className="mb-5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        On this page
      </p>

      <ul data-features-contents="" className="space-y-1">
        {FEATURES_SECTIONS.map((section) => (
          <li key={section.id}>
            <a
              href={`#${section.id}`}
              data-features-link={section.id}
              className="block rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              {section.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
