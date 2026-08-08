// The strings the enrichment toggle needs on both surfaces it appears on — the add-site
// form (`components/landing/site-url-field.tsx`) and the detail page's
// `components/crawls/enrichment-panel.tsx` — mirroring `lib/crawls/schedule-presets.ts`: one
// module holding copy two components would otherwise have to keep in sync by hand.
//
// **No pricing constant in this file, or anywhere in the frontend (PER-194's own [Cost]
// acceptance criterion).** `ENRICHMENT_HELP` below states the MECHANISM and an
// order-of-magnitude, in prose, matching the backend's own help text
// (`backend/app/features/websites/schemas.py`'s `_ENRICH_WITH_LLM_DESCRIPTION`) — neither
// side computes a number from token counts or a price-per-token constant, because neither
// commits to a number that would drift the day a model's price changes.

/** The checkbox / toggle's own label, used on both surfaces. */
export const ENRICHMENT_LABEL = "Summarize pages with Claude";

/**
 * The help text under the toggle. States the mechanism and an order of magnitude — never a
 * computed estimate — so a change to actual pricing never makes this sentence wrong. Mirrors
 * `backend/app/core/settings.py`'s own arithmetic comment for `crawl_enrich_max_chars`.
 */
export const ENRICHMENT_HELP =
  "One Claude Haiku 4.5 call per page, on the first 4,000 characters. On the order of cents for a 100-page run.";

/** The Runs-tab and Output-tab badge's visible text, when a run asked for enrichment and did
 * not get it — `lib/crawls/run-display.ts`'s `runEnrichmentFellBack` is the predicate that
 * decides when this shows at all. */
export const ENRICHMENT_FALLBACK_BADGE = "Enrichment unavailable";

/** The tooltip attached to the badge above — the full sentence, since the badge itself has to
 * stay short enough to sit beside `RunStatusIndicator` in a table cell. */
export const ENRICHMENT_FALLBACK_EXPLANATION =
  "Enrichment requested but unavailable — titles come from the page.";

/**
 * The Trends tab's "Titles & descriptions" tile/column copy when `metadata_changed` is `null`
 * — keyed by `RunIndexDiff.metadata_not_comparable_reason`
 * (`backend/app/features/runs/schemas.py`). Two entries, not one: the direction the toggle
 * flipped changes which sentence is true, and rendering the ticket's own sentence verbatim
 * for the case it describes (and its mirror for the other) is more honest than one sentence
 * that tries to cover both directions at once.
 */
export const METADATA_NOT_COMPARABLE: Record<"enrichment_enabled" | "enrichment_disabled", string> =
  {
    enrichment_enabled:
      "Enrichment turned on since the last comparison — titles and descriptions aren't comparable this run.",
    enrichment_disabled:
      "Enrichment turned off since the last comparison — titles and descriptions aren't comparable this run.",
  };
