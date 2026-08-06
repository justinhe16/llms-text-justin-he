"use client";

import { NumberTicker } from "@/components/magicui/number-ticker";
import { Skeleton } from "@/components/ui/skeleton";
import type { StatsTotals } from "@/lib/api/runs";
import { msToSeconds, successRatePercent } from "@/lib/crawls/stats-display";

import { EmptyCell } from "./empty-cell";

/** How many tiles the skeleton draws, and how many `TrendsStatTiles` renders. Named so the
 * two cannot drift and leave the loading state a different height from the loaded one. */
const TILE_COUNT = 4;

/**
 * One tile's value: either a number to count up to, or nothing at all.
 *
 * `null` is a first-class case here rather than something a caller pre-formats into a string,
 * because exactly one tile can be genuinely absent — success rate — and collapsing it to
 * `"—"` upstream would lose the distinction the API went to the trouble of making.
 */
interface StatTileProps {
  label: string;
  value: number | null;
  /** What a screen reader should say when `value` is `null`, e.g. "no runs in this window". */
  emptyLabel: string;
  /** Rendered immediately after the number: "%" or "s". */
  suffix?: string;
  decimalPlaces?: number;
}

/**
 * The four numbers above the charts.
 *
 * ## The animated number is hidden from screen readers
 *
 * `NumberTicker` drives a spring and writes `textContent` on every frame, so an assistive
 * technology watching that node would announce a stream of intermediate numbers on its way to
 * the real one. The ticker is therefore `aria-hidden` and the settled value is rendered once,
 * `sr-only`, beside it — the same trick `EmptyCell` uses for its dash, and for the same
 * reason: what is pleasant to watch and what is bearable to listen to are different things.
 */
function StatTile({ label, value, emptyLabel, suffix, decimalPlaces = 0 }: StatTileProps) {
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>

      <p className="mt-1 text-2xl font-medium tracking-tight text-foreground tabular-nums">
        {value === null ? (
          // The one branch this whole component exists to get right. `value === null` is an
          // explicit identity check, never `value || fallback`: `0` is falsy and is a real
          // measurement — a 0% success rate, a website whose every run failed — and `||`
          // would render it as "no data", which is the exact opposite of what it means.
          <EmptyCell label={emptyLabel} />
        ) : (
          <>
            <span aria-hidden="true">
              <NumberTicker value={value} decimalPlaces={decimalPlaces} />
              {suffix}
            </span>
            <span className="sr-only">
              {value.toFixed(decimalPlaces)}
              {suffix}
            </span>
          </>
        )}
      </p>
    </div>
  );
}

/** The tiles' loading state, same count and same box so the panel does not resize when the
 * numbers land. */
export function TrendsStatTilesSkeleton() {
  return (
    <div aria-hidden="true" className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {Array.from({ length: TILE_COUNT }, (_, index) => (
        <div key={index} className="rounded-xl border border-border bg-card px-4 py-3">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="mt-2 h-7 w-16" />
        </div>
      ))}
    </div>
  );
}

/**
 * Total runs, success rate, average duration and average pages over the selected window.
 *
 * Two-up below `md` and four-up above it: four numbers side by side at 375px would each get
 * about 80 pixels, which is not enough for "18.2s" plus its label without wrapping the label
 * onto three lines.
 */
export function TrendsStatTiles({ totals }: { totals: StatsTotals }) {
  // `successRatePercent` preserves `null` rather than coercing it — see its docstring, and
  // `StatTile` above for what the `null` becomes.
  const successRate = successRatePercent(totals.success_rate);

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <StatTile label="Total runs" value={totals.total_runs} emptyLabel="no runs" />

      <StatTile
        label="Success rate"
        value={successRate}
        // The API returns `null` here for exactly one reason, and this says which.
        emptyLabel="no runs in this window, so there is no success rate"
        suffix="%"
        decimalPlaces={1}
      />

      <StatTile
        label="Avg duration"
        // Seconds, not the milliseconds the API reports. One conversion, in one place
        // (lib/crawls/stats-display.ts), shared with the duration chart.
        value={msToSeconds(totals.avg_duration_ms)}
        emptyLabel="no duration recorded"
        suffix="s"
        decimalPlaces={1}
      />

      <StatTile label="Avg pages" value={totals.avg_pages} emptyLabel="no pages counted" />
    </div>
  );
}
