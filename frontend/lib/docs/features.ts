// The canonical list of sections on `/docs/features`, and the single place its contents-list
// ids are written down. Mirrors `sections.ts` and `architecture.ts`'s reason for existing —
// see ARCHITECTURE.md §8.4 — but this tab has no diagram to drive: it is prose, in document
// order, so this file is only the ordered `{id, label}` pairs `FeaturesContents` renders as
// links, and nothing else. No icons, no beams, no active-section highlighting.
//
// `id` is the heading's slug in `app/docs/features/page.mdx`, exactly as `sections.ts` and
// `architecture.ts` document for their own pages: `rehype-slug` (wired in `next.config.ts`)
// derives it from the heading text, nothing here recomputes it, and `scripts/smoke.mjs` is
// what catches the two drifting apart — the same gate, for a third file.

export type FeaturesSection = {
  /** The `id` of the `h2` this link points at, and its `github-slugger` slug. */
  id: string;
  /** The link's visible text. Matches the heading it points to, so the contents list reads
   *  as a table of contents rather than a second, differently-worded index. */
  label: string;
};

/** The six sections of `/docs/features`, in document order. */
export const FEATURES_SECTIONS: readonly FeaturesSection[] = [
  { id: "object-model", label: "Object model" },
  { id: "adding-a-site", label: "Adding a site" },
  { id: "schedules", label: "Schedules" },
  { id: "trends", label: "Trends" },
  { id: "limits", label: "Limits" },
  { id: "api", label: "API" },
] as const;
