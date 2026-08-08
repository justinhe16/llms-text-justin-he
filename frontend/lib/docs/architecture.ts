// The canonical topology `/docs/architecture` renders: seven deployed pieces and the eight
// edges between them. `components/docs/architecture-diagram.tsx` renders the diagram from
// these two arrays and nothing else — the same shape `lib/docs/sections.ts` gives the run
// tab's pipeline diagram, extended for a graph rather than a line.
//
// `sectionId` is a heading's slug in `app/docs/architecture/page.mdx`. `rehype-slug` (wired
// in `next.config.ts`) derives that slug from the heading text, exactly as it does for
// `app/docs/(run)/page.mdx`. Nothing recomputes it here, which means a renamed heading
// silently breaks the link between the two files — and that is exactly what
// `scripts/smoke.mjs` gates for this route too: it loads the rendered page and fails when a
// node's `sectionId` resolves to no heading.

import { Globe } from "lucide-react";

import { AnthropicMark } from "@/components/docs/anthropic-mark";
import {
  FastApiMark,
  FlyMark,
  NextjsMark,
  PostgresMark,
  PythonMark,
  RedisMark,
  SupabaseMark,
  VercelMark,
} from "@/components/docs/brand-marks";
import type { DocsSectionIcon } from "@/lib/docs/sections";

/** The ten `h2` slugs on `/docs/architecture`, in document order: a short section for each
 *  deployed piece, then the cross-cutting sections. Verified against `rehype-slug`'s actual
 *  output (`github-slugger`), not guessed — "Next.js" slugs to `nextjs`, not `next-js`. */
export const ARCHITECTURE_SECTION_IDS = [
  "shape",
  "nextjs",
  "fastapi",
  "job-queue",
  "arq-worker",
  "supabase",
  "request-path",
  "concurrency",
  "failure-handling",
  "trade-offs",
] as const;

export type ArchitectureSectionId = (typeof ARCHITECTURE_SECTION_IDS)[number];

type ArchitectureNodeShape = {
  /** Unique id. Edges reference it by `from`/`to`; `scripts/smoke.mjs` pairs a beam to the
   *  node it starts and ends at by the same id. */
  id: string;
  /** The node's visible caption — at most two short words, matching the pipeline node's
   *  constraint for the same reason: the grid has to fit a viewport at `lg`. */
  label: string;
  /** Where this piece runs, rendered under the label in a lighter weight. */
  sublabel: string;
  /** An sr-only continuation of `label`, never a replacement for it — see `description` in
   *  `lib/docs/sections.ts` for the WCAG 2.5.3 reasoning this repeats verbatim. */
  description: string;
  /** The `h2` this node scrolls to. Nearly 1:1 now: the only section two nodes still share
   *  is `arq-worker` (the worker itself, and the Anthropic call it is the one thing that
   *  could make) — see `ArchitectureDiagram`'s docstring for why that remaining overlap
   *  still rules out a lit "active" node here. */
  sectionId: ArchitectureSectionId;
  /** 1 = the left column (the request/response spine), 2 = the right column (the async
   *  fan-out: the queue, the worker, and what the worker calls). */
  column: 1 | 2;
  /** Grid row, 1-indexed top to bottom. Rows 4 and 5 of column 1 are deliberately unused —
   *  see `ArchitectureDiagram`'s docstring for what that empty channel is for. */
  row: number;
  /** One or two icons, rendered left to right in the node's icon row. */
  icons: readonly DocsSectionIcon[];
  /** Visible sub-items, rendered as small chips inside the node — for the worker only. The
   *  crawl, the scheduler tick, and the reaper all run inside the `worker` process; they are
   *  not separately deployed, so they are contents of this one node rather than nodes and
   *  beams of their own, which would draw a topology this system doesn't have. */
  subitems?: readonly string[];
};

/**
 * The seven deployed pieces, positioned on a 2-column grid. `column`/`row` are data about the
 * graph, not styling — they reach the DOM as `style={{ gridColumnStart, gridRowStart }}`
 * rather than a Tailwind `col-start-*` class, because `col-start-${n}` built from a variable
 * is invisible to Tailwind's class scanner and a static class map would be a second place for
 * these coordinates to live.
 */
export const ARCHITECTURE_NODES = [
  {
    id: "browser",
    label: "Browser",
    sublabel: "the reader",
    description: "the client that requests a page",
    sectionId: "request-path",
    column: 1,
    row: 1,
    icons: [Globe],
    subitems: undefined,
  },
  {
    id: "vercel",
    label: "Next.js",
    sublabel: "Vercel",
    description: "the App Router, and the route handlers that proxy to Fly",
    sectionId: "nextjs",
    column: 1,
    row: 2,
    icons: [VercelMark, NextjsMark],
    subitems: undefined,
  },
  {
    id: "api",
    label: "FastAPI",
    sublabel: "Fly.io",
    description: "the app process, one of two on the same image",
    sectionId: "fastapi",
    column: 1,
    row: 3,
    icons: [FlyMark, FastApiMark],
    subitems: undefined,
  },
  {
    id: "redis",
    label: "Job queue",
    sublabel: "Upstash Redis",
    description: "the ARQ queue a crawl job waits in before the worker claims it",
    sectionId: "job-queue",
    column: 2,
    row: 4,
    icons: [RedisMark],
    subitems: undefined,
  },
  {
    id: "worker",
    label: "ARQ worker",
    sublabel: "Fly.io",
    description: "the worker process, running the crawl, the scheduler tick, and the reaper",
    sectionId: "arq-worker",
    column: 2,
    row: 5,
    icons: [FlyMark, PythonMark],
    subitems: ["Crawl", "Scheduler tick", "Reaper"],
  },
  {
    id: "supabase",
    label: "Supabase",
    sublabel: "Postgres · Storage",
    description: "Postgres over asyncpg, Auth, and Storage",
    sectionId: "supabase",
    column: 1,
    row: 6,
    icons: [SupabaseMark, PostgresMark],
    subitems: undefined,
  },
  {
    id: "anthropic",
    label: "Anthropic API",
    sublabel: "off by default",
    description: "the model call the worker can make, and does not today",
    sectionId: "arq-worker",
    column: 2,
    row: 6,
    icons: [AnthropicMark],
    subitems: undefined,
  },
] as const satisfies readonly ArchitectureNodeShape[];

export type ArchitectureNodeId = (typeof ARCHITECTURE_NODES)[number]["id"];

export type ArchitectureEdge = {
  from: ArchitectureNodeId;
  to: ArchitectureNodeId;
  /** True for exactly one edge — worker→anthropic. See `ArchitectureDiagram`'s docstring for
   *  why that beam is drawn dashed rather than animated. */
  dashed?: boolean;
  /** True for the worker→redis edge only, so its gradient sweeps worker-to-redis instead of
   *  the default top-to-bottom — the direction this loop actually flows, and the mirror of
   *  the redis→worker beam it runs beside. */
  reverse?: boolean;
  /** Horizontal pixel offset applied to both ends of this beam. `redis` and `worker` sit in
   *  the same grid column, so the queue→worker beam and this new worker→queue one would
   *  otherwise draw on top of each other. Only worker→redis sets this, which turns the pair
   *  into two parallel lanes instead of one indistinguishable line — see
   *  `ArchitectureDiagram` for where it is applied, and `scripts/smoke.mjs`'s
   *  `misalignedArchBeams` for how the alignment check accounts for it. */
  laneOffsetPx?: number;
};

/**
 * The eight edges of the topology, in the order the diagram draws their beams. `from`/`to`
 * are `ArchitectureNodeId`, so a typo in either fails `tsc` instead of silently dropping a
 * beam — the same "a cross-file id needs a gate" argument ARCHITECTURE.md §8.4 makes about
 * `sectionId`, applied here to a join the compiler CAN check.
 *
 * Order matters beyond readability: `scripts/smoke.mjs` reads this exact array (via
 * `data-arch-edges` on the diagram's container, built with one `.map().join(",")` from this
 * constant) and pairs beam *i* in DOM order with edge *i* here, so the two can never drift
 * apart.
 *
 * `redis→worker` and `worker→redis` are the queue loop: the worker claims a job one way, and
 * its own scheduler tick enqueues one back the other way. Both are real, both are animated —
 * neither is the dashed, unanimated exception `worker→anthropic` is.
 */
export const ARCHITECTURE_EDGES: readonly ArchitectureEdge[] = [
  { from: "browser", to: "vercel" },
  { from: "vercel", to: "api" },
  { from: "api", to: "redis" },
  { from: "redis", to: "worker" },
  { from: "worker", to: "redis", reverse: true, laneOffsetPx: 7 },
  { from: "api", to: "supabase" },
  { from: "worker", to: "supabase" },
  { from: "worker", to: "anthropic", dashed: true },
];
