"use client";

import { useMemo, useRef } from "react";

import { AnimatedBeam } from "@/components/magicui/animated-beam";
import { DOCS_SECTIONS } from "@/lib/docs/sections";
import { scrollToSection } from "@/lib/docs/scroll-to-section";
import { useActiveSection } from "@/lib/docs/use-active-section";
import { cn } from "@/lib/utils";

/**
 * The pipeline diagram in the left column of `/docs`: seven nodes in a vertical chain, six
 * `AnimatedBeam`s between them.
 *
 * Markup, near enough. The two pieces of behaviour it needs both live in `lib/docs/` per
 * ARCHITECTURE.md §8.4 — `useActiveSection` decides which node is lit, `scrollToSection`
 * decides how the page moves and whether that movement is animated. Nothing here knows what
 * a section *is*; `DOCS_SECTIONS` is the only source of ids, labels and icons.
 *
 * ## It is an aid, not the documentation
 *
 * The right column is complete prose on its own, and this renders as a `<nav>` of links into
 * it. Delete this component and `/docs` loses a picture, not a fact. That is also why the
 * beams are `aria-hidden` and the labels repeat words the headings already carry.
 *
 * ## Why the beams are direct children of the container
 *
 * Each `AnimatedBeam` renders one absolutely-positioned `<svg>` spanning the whole
 * container, which is why it sits outside the flex flow and needs a `relative` parent it can
 * measure. Keeping them as *direct* children also gives `scripts/smoke.mjs` an exact
 * selector for the six beams — `[data-docs-diagram] > svg` — that cannot accidentally pick
 * up the seven icon `<svg>`s nested inside the buttons.
 */
export function DocsDiagram() {
  const containerRef = useRef<HTMLDivElement>(null);

  // One stable ref object per node. `useRef` cannot be called in a loop, and a plain array
  // rebuilt each render would hand `AnimatedBeam` a new object every time and re-run its
  // measuring effect on every parent render. These are created once and mutated by the
  // callback refs below.
  const nodeRefs = useMemo(
    () => DOCS_SECTIONS.map(() => ({ current: null as HTMLElement | null })),
    [],
  );

  const sectionIds = useMemo(() => DOCS_SECTIONS.map((section) => section.id), []);
  const activeId = useActiveSection(sectionIds);

  return (
    <nav aria-label="Pipeline stages" className="lg:sticky lg:top-14">
      {/* A <p>, not a heading. This column renders before the article in the DOM, so an <h2>
          here would land above the page's <h1> and leave the document outline starting at
          level 2. The <nav>'s aria-label already names the landmark, so the heading bought
          nothing a screen reader did not already have. */}
      <p className="mb-5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        How a run works
      </p>

      <div
        ref={containerRef}
        data-docs-diagram=""
        className="relative flex flex-col items-stretch gap-4 lg:gap-5"
      >
        {DOCS_SECTIONS.map((section, index) => {
          const Icon = section.icon;
          const isActive = section.id === activeId;

          return (
            <button
              key={section.id}
              type="button"
              ref={(element) => {
                nodeRefs[index].current = element;
              }}
              data-docs-node={section.id}
              data-active={isActive ? "" : undefined}
              aria-current={isActive ? "location" : undefined}
              onClick={() => scrollToSection(section.id)}
              className={cn(
                // `z-10` keeps the node above the beam svgs, which span the whole container.
                "relative z-10 flex items-center gap-3 rounded-lg border bg-card px-3 py-2.5 text-left transition-colors outline-none",
                "focus-visible:ring-3 focus-visible:ring-ring/50",
                isActive
                  ? "border-primary/40 text-foreground"
                  : "border-border text-muted-foreground hover:border-primary/25 hover:text-foreground",
              )}
            >
              <span
                className={cn(
                  "flex size-7 shrink-0 items-center justify-center rounded-md transition-colors",
                  isActive ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
                )}
              >
                <Icon className="size-4" aria-hidden="true" />
              </span>
              <span className="text-sm font-medium">{section.label}</span>
              {/* The accessible name is the visible label plus this, in that order — never
                  an `aria-label` that replaces it (WCAG 2.5.3, and see `description` in
                  lib/docs/sections.ts). A native <button> needs no key handling: Enter and
                  Space both fire `click`, and the browser suppresses Space's page scroll.
                  Do not copy the `onKeyDown` from lib/crawls/row-activation.ts, which exists
                  only because a <tr> is not a button. */}
              <span className="sr-only">
                , stage {index + 1} of {DOCS_SECTIONS.length}: {section.description}
              </span>
            </button>
          );
        })}

        {/* Six beams for seven nodes — derived, so a stage added to DOCS_SECTIONS brings its
            own connection rather than needing a second edit here. */}
        {DOCS_SECTIONS.slice(0, -1).map((section, index) => (
          <AnimatedBeam
            key={`${section.id}-beam`}
            containerRef={containerRef}
            fromRef={nodeRefs[index]}
            toRef={nodeRefs[index + 1]}
            orientation="vertical"
            duration={4}
            pathWidth={1.5}
          />
        ))}
      </div>
    </nav>
  );
}
