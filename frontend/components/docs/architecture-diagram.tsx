"use client";

import { useMemo, useRef } from "react";

import { AnimatedBeam } from "@/components/magicui/animated-beam";
import { ARCHITECTURE_EDGES, ARCHITECTURE_NODES } from "@/lib/docs/architecture";
import { scrollToSection } from "@/lib/docs/scroll-to-section";
import { cn } from "@/lib/utils";

/**
 * The topology diagram in the left column of `/docs/architecture`: seven nodes on a 2-column
 * grid, eight `AnimatedBeam`s between them.
 *
 * It is an aid, not the documentation — the same claim `docs-diagram.tsx` makes about the
 * pipeline. The right column is complete prose on its own; delete this component and the tab
 * loses a picture, not a fact. `shape`, `concurrency`, `failure-handling` and `trade-offs`
 * have no node pointing at them for exactly that reason: this diagram draws *what is
 * deployed where*, not a table of contents, and a picture cannot draw a trade-off.
 *
 * ## No active/lit state
 *
 * `DocsDiagram` lights the node under the reader's scroll position because its mapping from
 * node to section is 1:1 and ordered — stage 3 is always "Selection". This diagram's mapping
 * is nearly that now, but not quite: the worker node and the Anthropic node both point at
 * `arq-worker`, because the Anthropic call is a thing the worker — and only the worker —
 * would make. One remaining shared section is enough that no single node could honestly be
 * "the" active one, so this component still carries no lit state; and the diagram is an aid
 * rather than a table of contents regardless, so a perfect 1:1 mapping was never the bar.
 * Every node still scrolls on click — the `[Smoke]` gate that gave the pipeline diagram its
 * anchor test applies here too, and a `data-arch-section` only a test reads is a dead
 * relationship no test should be gating.
 *
 * ## The grid, and the empty channel
 *
 * `ARCHITECTURE_NODES` carries explicit `column`/`row` data, rendered as inline
 * `style={{ gridColumnStart, gridRowStart }}` on a `grid-cols-2` container — not a Tailwind
 * `col-start-${n}` class, which a variable makes invisible to Tailwind's scanner. Column 1,
 * rows 4 and 5 are deliberately empty: that gap is what lets the `api → supabase` beam run
 * straight down an unobstructed channel instead of passing behind the Redis or worker nodes
 * sitting in column 2 at those same rows. It is why the fan-out (queue, worker) sits in
 * column 2 rather than the spine being centred with branches either side.
 *
 * ## Explicit edges, not a derived chain
 *
 * `DOCS_SECTIONS.length - 1` gives the pipeline its six beams because a pipeline is a line.
 * This is a graph — `api` feeds both `redis` and `supabase`, `worker` feeds both `supabase`
 * and `anthropic`, and `redis` and `worker` feed each other (the worker claims a job one way;
 * its own scheduler tick enqueues one back the other way) — so the connections are named
 * once, explicitly, in `ARCHITECTURE_EDGES`, and nothing here recomputes them.
 *
 * ## Two beams, one pair of nodes
 *
 * `redis` and `worker` sit in the same grid column, so `redis→worker` and `worker→redis`
 * would draw on identical paths. `worker→redis` alone carries `laneOffsetPx`, fed to
 * `AnimatedBeam` as `startXOffset`/`endXOffset` so that one beam draws a few pixels to the
 * side instead of on top of the other — two parallel lanes, not one line with two pulses
 * fighting over it. It also carries `reverse`, so its gradient travels worker-to-redis
 * instead of the default top-to-bottom. `scripts/smoke.mjs`'s alignment check reads the same
 * offset back off `data-arch-edges` rather than assuming every beam lands dead-centre.
 *
 * ## Why the beams are direct children of the container
 *
 * Same reason as `docs-diagram.tsx`: each `AnimatedBeam` renders one absolutely-positioned
 * `<svg>` spanning the whole container, so it has to sit outside the grid flow against a
 * `relative` parent it can measure. Keeping them as *direct* children gives
 * `scripts/smoke.mjs` an exact selector for the eight beams — `[data-arch-diagram] > svg` —
 * that cannot accidentally pick up the icon `<svg>`s nested inside the buttons.
 * `data-arch-edges` on that same container is a comma-joined, `>`-delimited encoding of
 * `ARCHITECTURE_EDGES` (`"browser>vercel,vercel>api,…worker>redis:7,…"`), built with one
 * `.map().join(",")` so it cannot drift from the array the beams are actually built from; the
 * smoke test reads it to pair beam *i* in DOM order with edge *i* in this list.
 */
export function ArchitectureDiagram() {
  const containerRef = useRef<HTMLDivElement>(null);

  // One stable ref object per node, and one stable ref-setter per node — see
  // `docs-diagram.tsx` for both comments in full. `useRef` cannot be called in a loop, and an
  // inline `ref={(el) => ...}` is a new function every render, which would detach and
  // reattach every node's ref on every parent render for nothing.
  const nodeRefs = useMemo(
    () => ARCHITECTURE_NODES.map(() => ({ current: null as HTMLElement | null })),
    [],
  );
  const setNodeRef = useMemo(
    () =>
      nodeRefs.map((ref) => (element: HTMLButtonElement | null) => {
        ref.current = element;
      }),
    [nodeRefs],
  );

  const refById = useMemo(
    () => new Map(ARCHITECTURE_NODES.map((node, index) => [node.id, nodeRefs[index]] as const)),
    [nodeRefs],
  );

  const edgeAttr = useMemo(
    () =>
      ARCHITECTURE_EDGES.map(
        (edge) => `${edge.from}>${edge.to}${edge.laneOffsetPx ? `:${edge.laneOffsetPx}` : ""}`,
      ).join(","),
    [],
  );

  return (
    <nav aria-label="Deployed pieces" className="lg:sticky lg:top-14">
      {/* A <p>, not a heading — this column renders before the article in the DOM, so an
          <h2> here would start the document outline at level 2 (see docs-diagram.tsx). */}
      <p className="mb-5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        What is deployed where
      </p>

      <div
        ref={containerRef}
        data-arch-diagram=""
        data-arch-edges={edgeAttr}
        className="relative grid grid-cols-2 gap-x-3 gap-y-4 sm:gap-x-4 lg:gap-y-5"
      >
        {ARCHITECTURE_NODES.map((node, index) => (
          <button
            key={node.id}
            type="button"
            ref={setNodeRef[index]}
            data-arch-node={node.id}
            data-arch-section={node.sectionId}
            onClick={() => scrollToSection(node.sectionId)}
            style={{ gridColumnStart: node.column, gridRowStart: node.row }}
            className={cn(
              // `z-10` keeps the node above the beam svgs, which span the whole container.
              "relative z-10 flex min-w-0 flex-col items-center gap-1 rounded-lg border border-border bg-card px-2 py-2.5 text-center transition-colors outline-none",
              "hover:border-primary/25 focus-visible:ring-3 focus-visible:ring-ring/50",
            )}
          >
            <span className="flex items-center gap-1.5 text-muted-foreground">
              {node.icons.map((Icon, iconIndex) => (
                <Icon key={iconIndex} className="size-4" aria-hidden="true" />
              ))}
            </span>
            <span className="text-xs font-medium text-foreground sm:text-sm">{node.label}</span>
            <span className="text-[0.6875rem] text-muted-foreground">{node.sublabel}</span>
            {/* Visible, not sr-only: the worker's subitems are the answer to "where does the
                reaper run?", and that question is exactly what a picture is for. */}
            {node.subitems ? (
              <span className="mt-0.5 flex flex-col items-center gap-0.5">
                {node.subitems.map((item) => (
                  <span
                    key={item}
                    className="rounded-full border border-border/70 bg-muted px-1.5 py-0.5 text-[0.5625rem] leading-none whitespace-nowrap text-muted-foreground"
                  >
                    {item}
                  </span>
                ))}
              </span>
            ) : null}
            {/* The accessible name is the visible label plus this, in that order — never an
                `aria-label` that replaces it. See `description` in lib/docs/sections.ts for
                the WCAG 2.5.3 reasoning, repeated verbatim for this diagram. */}
            <span className="sr-only">, {node.description}</span>
          </button>
        ))}

        {ARCHITECTURE_EDGES.map((edge) => {
          const fromRef = refById.get(edge.from);
          const toRef = refById.get(edge.to);
          // No `!` here: a bad id drops this one beam rather than throwing, and the smoke
          // test's beam-count assertion is what catches the drop.
          if (!fromRef || !toRef) return null;

          return (
            <AnimatedBeam
              key={`${edge.from}-${edge.to}`}
              containerRef={containerRef}
              fromRef={fromRef}
              toRef={toRef}
              orientation="vertical"
              duration={4}
              pathWidth={1.5}
              reverse={edge.reverse}
              startXOffset={edge.laneOffsetPx ?? 0}
              endXOffset={edge.laneOffsetPx ?? 0}
              {...(edge.dashed
                ? {
                    className: "[&_path]:[stroke-dasharray:4_4]",
                    // The gradient stops are the path's own colour, so the beam never appears
                    // to carry anything. CRAWL_ENRICH_WITH_LLM is false in the deployed
                    // service; a travelling pulse would claim a call that does not happen.
                    gradientStartColor: "var(--border)",
                    gradientStopColor: "var(--border)",
                  }
                : {})}
            />
          );
        })}
      </div>
    </nav>
  );
}
