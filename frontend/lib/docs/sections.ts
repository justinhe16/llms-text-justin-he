// The canonical list of pipeline stages on /docs, and the single place an anchor id is
// written down.
//
// One entry per diagram node, in execution order. `components/docs/docs-diagram.tsx`
// renders the diagram from this array and nothing else — the node count, the labels, the
// icons and the scroll targets all come from here, so adding a stage is one edit rather
// than four.
//
// `id` is the heading's slug in `app/docs/page.mdx`. `rehype-slug` (wired in
// `next.config.ts`) derives that slug from the heading text with `github-slugger`, so
// `## Discovery` becomes `id="discovery"`. Nothing recomputes it here, which means a
// renamed heading silently breaks the link between the two files — and that is exactly what
// `scripts/smoke.mjs` gates: it loads the rendered page and fails when a node's id resolves
// to no heading. Rename a heading and the build stays green; the smoke test does not.

import { Download, FileCode2, FileText, Link2, ListFilter, Network } from "lucide-react";

import { AnthropicMark } from "@/components/docs/anthropic-mark";

/**
 * What a section's icon has to be: a component taking `className`. Both `lucide-react`'s
 * icons and `AnthropicMark` satisfy it, which is what lets one array hold both.
 *
 * This is the one place `lib/docs/` reaches into `components/` (ARCHITECTURE.md §8.4 puts
 * a feature's non-visual logic here). The alternative — storing raw SVG path data in this
 * file and reassembling it in the diagram — would move a picture into a data module and
 * split one icon's definition across two files to satisfy a directory rule. The icon is
 * part of the canonical entry; the ticket for this page says so.
 */
export type DocsSectionIcon = React.ComponentType<{
  className?: string;
  "aria-hidden"?: boolean | "true" | "false";
}>;

export type DocsSection = {
  /** The `id` of the `h2` this node scrolls to, and its `github-slugger` slug. */
  id: string;
  /** The node's caption. Two to four words, a noun phrase, never a sentence — the diagram
   *  has to fit a viewport at `lg` without scrolling, and that is the constraint that
   *  keeps it to seven nodes with short labels. */
  label: string;
  /**
   * What the stage does, in a fragment that reads as a continuation of `label`.
   *
   * Rendered `sr-only` *inside* the button rather than set as its `aria-label`. An
   * `aria-label` would replace the accessible name with text that does not contain the
   * visible label — a WCAG 2.5.3 "Label in Name" failure, and a real one: it breaks speech
   * input, where a user says the words they can see. Appending instead keeps the visible
   * label at the front of the name and adds detail behind it.
   */
  description: string;
  icon: DocsSectionIcon;
};

/**
 * The seven stages a run passes through, in order. Six `AnimatedBeam`s connect them, one
 * between each adjacent pair — `DOCS_SECTIONS.length - 1` by construction, never a second
 * hardcoded 6.
 */
export const DOCS_SECTIONS: readonly DocsSection[] = [
  {
    id: "input",
    label: "Seed URL",
    description: "the URL a website record holds",
    icon: Link2,
  },
  {
    id: "discovery",
    label: "URL discovery",
    description: "sitemaps, then robots.txt, then seed-page links",
    icon: Network,
  },
  {
    id: "selection",
    label: "URL selection",
    description: "drop rules, scoring, then the page cap",
    icon: ListFilter,
  },
  {
    id: "fetch",
    label: "Page fetch",
    description: "six caps, and an SSRF check on every URL",
    icon: Download,
  },
  {
    id: "extract",
    label: "Content extraction",
    description: "trafilatura returns markdown, a title and a description",
    icon: FileText,
  },
  {
    id: "summarize",
    label: "Optional summary",
    description: "a configuration flag gates a model pass",
    icon: AnthropicMark,
  },
  {
    id: "output",
    label: "llms.txt output",
    description: "llms.txt and llms-full.txt",
    icon: FileCode2,
  },
] as const;
