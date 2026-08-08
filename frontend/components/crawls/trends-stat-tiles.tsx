"use client";

import { NumberTicker } from "@/components/magicui/number-ticker";
import { Skeleton } from "@/components/ui/skeleton";
import type { StatsLatest, StatsTotals } from "@/lib/api/runs";
import { METADATA_NOT_COMPARABLE } from "@/lib/crawls/enrichment-copy";
import { successRatePercent } from "@/lib/crawls/stats-display";

import { EmptyCell } from "./empty-cell";

/** How many output tiles `TrendsOutputTiles` renders, and how many the skeleton draws for
 * that row. Named so the two cannot drift and leave the loading state a different height from
 * the loaded one. PER-194 split the old "Changed" tile into "Content changed" and "Titles &
 * descriptions", taking this from four to five. */
const OUTPUT_TILE_COUNT = 5;

/**
 * One tile's value: either a number to count up to, or nothing at all.
 *
 * `null` is a first-class case here rather than something a caller pre-formats into a string,
 * because more than one tile can be genuinely absent — a success rate with no runs to compute
 * it from, an added/removed/changed count with no comparison to report — and collapsing any of
 * them to `"—"` upstream would lose the distinction the API went to the trouble of making.
 */
interface StatTileProps {
  label: string;
  value: number | null;
  /** What a screen reader should say when `value` is `null`, e.g. "no runs in this window". */
  emptyLabel: string;
  /** Rendered immediately before the number, inside both the visible and the `sr-only` copy —
   * "+" for an added-pages tile, "-" for a removed-pages one. Absent for a plain count. */
  prefix?: string;
  /** Rendered immediately after the number: "%" or "s". */
  suffix?: string;
  decimalPlaces?: number;
  /** Colours the number `text-status-completed` (`"positive"`) or `text-status-failed`
   * (`"negative"`) — the same two tokens the outcome bar and the change chart use, so green
   * and red mean the same thing everywhere in this panel. Omitted for a neutral count. */
  tone?: "positive" | "negative";
}

/**
 * One tile: a label, an animated number, or an empty state.
 *
 * Exported (it was a private helper before PER-193) because `TrendsOutputTiles` below now
 * needs it directly rather than through a single hard-coded row of four.
 *
 * ## The animated number is hidden from screen readers
 *
 * `NumberTicker` drives a spring and writes `textContent` on every frame, so an assistive
 * technology watching that node would announce a stream of intermediate numbers on its way to
 * the real one. The ticker is therefore `aria-hidden` and the settled value is rendered once,
 * `sr-only`, beside it — the same trick `EmptyCell` uses for its dash, and for the same
 * reason: what is pleasant to watch and what is bearable to listen to are different things.
 */
export function StatTile({
  label,
  value,
  emptyLabel,
  prefix,
  suffix,
  decimalPlaces = 0,
  tone,
}: StatTileProps) {
  const toneClass =
    tone === "positive"
      ? "text-status-completed"
      : tone === "negative"
        ? "text-status-failed"
        : "text-foreground";

  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>

      <p className={`mt-1 text-2xl font-medium tracking-tight tabular-nums ${toneClass}`}>
        {value === null ? (
          // The one branch this whole component exists to get right. `value === null` is an
          // explicit identity check, never `value || fallback`: `0` is falsy and is a real
          // measurement — a 0% success rate, a website whose every run failed — and `||`
          // would render it as "no data", which is the exact opposite of what it means.
          <EmptyCell label={emptyLabel} />
        ) : (
          <>
            <span aria-hidden="true">
              {prefix}
              <NumberTicker value={value} decimalPlaces={decimalPlaces} />
              {suffix}
            </span>
            <span className="sr-only">
              {prefix}
              {value.toFixed(decimalPlaces)}
              {suffix}
            </span>
          </>
        )}
      </p>
    </div>
  );
}

/** The output tiles' loading state, plus the health line's own height — one box shape for
 * both, so the panel does not resize when the numbers land. */
export function TrendsStatTilesSkeleton() {
  return (
    <div aria-hidden="true" className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {Array.from({ length: OUTPUT_TILE_COUNT }, (_, index) => (
          <div key={index} className="rounded-xl border border-border bg-card px-4 py-3">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="mt-2 h-7 w-16" />
          </div>
        ))}
      </div>
      <Skeleton className="h-4 w-36" />
    </div>
  );
}

/** The `emptyLabel` for "Added"/"Removed" — and the fallback the two diff-detail helpers
 * below reach for when there is no comparison at all — which, unlike "Pages in index",
 * depend on whether a comparison exists rather than merely on whether a completed run does. */
function diffEmptyLabel(latest: StatsLatest | null): string {
  if (latest === null) return "no completed run in this window";
  switch (latest.diff_state) {
    case "first_run":
      return "nothing to compare against yet";
    case "not_recorded":
      return "no comparison recorded for this run";
    case "compared":
      // Unreachable in practice — `diff` is non-null exactly when `diff_state` is
      // `"compared"` — but every branch needs a string, and this one is honest about why a
      // caller would never actually see it.
      return "no comparison recorded for this run";
  }
}

/** The `emptyLabel` for "Content changed" (PER-194) — `diffEmptyLabel` when there is no
 * comparison at all, else the one reason `diff.content_changed` can still be `null` inside a
 * real comparison: the previous run recorded no content hashes (it predates
 * `RUN_STATS_VERSION` 8). Mirrors the footer's own `urls_discovered_delta` treatment. */
function contentChangedEmptyLabel(latest: StatsLatest | null): string {
  if (latest?.diff == null) return diffEmptyLabel(latest);
  return "the previous run recorded no page fingerprints";
}

/** The `emptyLabel` for "Titles & descriptions" (PER-194) — `diffEmptyLabel` when there is no
 * comparison at all, else `METADATA_NOT_COMPARABLE`'s sentence for whichever direction the
 * enrichment toggle flipped since the previous run. */
function metadataChangedEmptyLabel(latest: StatsLatest | null): string {
  const diff = latest?.diff ?? null;
  if (diff === null) return diffEmptyLabel(latest);
  if (diff.metadata_not_comparable_reason) {
    return METADATA_NOT_COMPARABLE[diff.metadata_not_comparable_reason];
  }
  // Unreachable in practice — `metadata_changed` is null iff `metadata_not_comparable_reason`
  // is set — the identical "every branch needs a string" reasoning `diffEmptyLabel` gives.
  return "no comparison recorded for this run";
}

/**
 * The five PRIMARY tiles: what the newest completed run's index looks like, and what changed
 * in it — the output the ticket asks this panel to lead with, ahead of crawler health.
 * PER-194 split the old single "Changed" tile into "Content changed" (mode-independent) and
 * "Titles & descriptions" (not-comparable across an enrichment mode change).
 *
 * Two-up below `md` and five-up above it. The sibling docstring this comment used to carry
 * argued four-up would not fit at a 375px mobile width — this panel stays two-up below `md`
 * for exactly that reason, unchanged by the fifth tile; `md` (768px) and up have the room a
 * 375px phone does not.
 */
export function TrendsOutputTiles({ latest }: { latest: StatsLatest | null }) {
  const diff = latest?.diff ?? null;
  const emptyLabel = diffEmptyLabel(latest);

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      <StatTile
        label="Pages in index"
        value={latest?.index_pages ?? null}
        emptyLabel="no completed run in this window"
      />

      <StatTile
        label="Added"
        value={diff?.pages_added ?? null}
        emptyLabel={emptyLabel}
        prefix={diff !== null && diff.pages_added > 0 ? "+" : undefined}
        tone={diff !== null && diff.pages_added > 0 ? "positive" : undefined}
      />

      <StatTile
        label="Removed"
        value={diff?.pages_removed ?? null}
        emptyLabel={emptyLabel}
        prefix={diff !== null && diff.pages_removed > 0 ? "-" : undefined}
        tone={diff !== null && diff.pages_removed > 0 ? "negative" : undefined}
      />

      <StatTile
        label="Content changed"
        value={diff?.content_changed ?? null}
        emptyLabel={contentChangedEmptyLabel(latest)}
      />

      <StatTile
        label="Titles & descriptions"
        value={diff?.metadata_changed ?? null}
        emptyLabel={metadataChangedEmptyLabel(latest)}
      />
    </div>
  );
}

/**
 * The demoted health summary: total runs and success rate, in one quiet line rather than a
 * row of tiles. Present, because a broken crawl schedule is still worth surfacing on this
 * panel — but secondary, because the tab now reports on the artifact first.
 */
export function TrendsHealthRow({ totals }: { totals: StatsTotals }) {
  // `successRatePercent` preserves `null` rather than coercing it — see its docstring.
  const successRate = successRatePercent(totals.success_rate);

  return (
    <p className="text-xs text-muted-foreground">
      <span className="tabular-nums">{totals.total_runs.toLocaleString()}</span>{" "}
      {totals.total_runs === 1 ? "run" : "runs"}
      {successRate === null ? (
        <>
          {" · "}
          <EmptyCell label="no runs in this window, so there is no success rate" />
        </>
      ) : (
        <>
          {" · "}
          <span className="tabular-nums">{successRate.toFixed(1)}%</span> success
        </>
      )}
    </p>
  );
}
