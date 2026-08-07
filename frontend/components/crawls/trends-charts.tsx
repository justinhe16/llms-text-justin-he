"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import type { StatsBucket } from "@/lib/api/runs";
import { signedCount, type OutcomeBreakdown, type TrendPoint } from "@/lib/crawls/stats-display";

// ---------------------------------------------------------------------------------------
// Colour
// ---------------------------------------------------------------------------------------
//
// Every colour below is a `var(--…)` reading a custom property declared in `:root` by
// app/globals.css — never a Tailwind utility, and never a class name assembled at runtime.
//
// That is not a style preference, it is the only spelling that works in both directions
// here. Recharts paints SVG through `fill`/`stroke` attributes, which take colour values and
// not class names, so a utility would have to be resolved to a value anyway. And the runtime-
// assembled alternative (`` `bg-status-${token}` ``, the mistake run-status-indicator.tsx
// documents at length) fails silently: Tailwind builds its stylesheet by scanning source
// files for literal class strings, so a name that only ever exists as a template literal is
// simply absent from the compiled CSS and the element paints transparent — invisible to tsc,
// to eslint and to `next build`. `:root` custom properties have no such failure mode; that
// block is plain CSS and is emitted whole, whether or not anything references it.
//
// The two status colours are the SAME tokens the dots, badges and error surfaces use
// (`--status-completed`, `--status-failed`), so a red segment in this chart means exactly
// what a red dot means in the Runs table. Recolouring a status stays a one-line edit in
// globals.css, and this file is not an exception to it.
//
// PER-193's `ChangeChart` extends this rather than starting a second convention: its `added`
// series reuses `--status-completed` and its `removedNegative` series reuses `--status-failed`
// — the same green/red the outcome bar below already means "succeeded" and "failed" with, now
// carrying "grew" and "shrank" instead. One reader learns one meaning for each colour across
// the whole panel rather than two unrelated ones.

const INDEX_SIZE_CHART_CONFIG = {
  indexKb: { label: "Index size (KB)", color: "var(--chart-1)" },
} satisfies ChartConfig;

const CHANGE_CHART_CONFIG = {
  added: { label: "Pages added", color: "var(--status-completed)" },
  removedNegative: { label: "Pages removed", color: "var(--status-failed)" },
} satisfies ChartConfig;

const OUTCOME_CHART_CONFIG = {
  completed: { label: "Completed", color: "var(--status-completed)" },
  failed: { label: "Failed", color: "var(--status-failed)" },
  // Amber, matching `queued` and `running` in the Runs table — the two phases of "still
  // going" share one token there (lib/crawls/row-status.ts) and share this segment here.
  inProgress: { label: "In progress", color: "var(--status-processing)" },
} satisfies ChartConfig;

// Recharts animates a series on mount by default, over 1.5s. It is switched off on every
// series below, for two reasons. The ticket allows exactly one flourish on this panel — the
// stat tiles' `NumberTicker` — and three charts sweeping themselves in underneath four
// counting numbers is the "noisy" it was contrasted against. Second, a chart that animates
// has no single moment at which it is "rendered", which makes it unverifiable in a browser
// without sleeping for an arbitrary interval and hoping.
const ANIMATE = false;

// ---------------------------------------------------------------------------------------
// Tooltips
// ---------------------------------------------------------------------------------------

/** Recharts hands the tooltip its raw payload rows; this is the one field of them we read. */
function trendPointOf(value: unknown): TrendPoint | null {
  return typeof value === "object" && value !== null && "label" in value
    ? (value as TrendPoint)
    : null;
}

/**
 * The tooltip heading for a time-series chart: the bucket's fuller label ("Aug 5, 14:00"),
 * not the abbreviated axis tick.
 *
 * Read off the row's own pre-computed `label` rather than re-formatted here, so the tick and
 * the tooltip cannot disagree about which bucket they describe — `toTrendPoints`
 * (lib/crawls/stats-display.ts) derives both from the same `t` and the same `bucket`.
 */
function bucketTooltipLabel(
  _label: unknown,
  payload: readonly { payload?: unknown }[]
): string {
  return trendPointOf(payload[0]?.payload)?.label ?? "";
}

// ---------------------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------------------

/**
 * One chart in its card, with the "no runs" overlay.
 *
 * The overlay is why this wrapper exists rather than three ad-hoc `<div>`s. When a window
 * contains no runs the API still returns a full, zero-filled series, so all three charts
 * render perfectly: a flat line along the axis. That is *correct* and it is also
 * indistinguishable, at a glance, from a site that ran constantly and crawled zero pages
 * every time. The chart stays (its axis is real, and its flatness is the actual shape of the
 * data) and a quiet label says what the flatness means.
 *
 * `aria-hidden` on the chart underneath, when empty, so the overlay's sentence is the one
 * thing announced rather than a plateau of zeroes read out bucket by bucket.
 */
function ChartCard({
  title,
  description,
  isEmpty,
  emptyLabel = "No runs in this window",
  children,
}: {
  title: string;
  description: string;
  isEmpty: boolean;
  /** What the overlay says when `isEmpty`. Defaults to the outcome chart's own wording;
   * `IndexSizeChart` and `ChangeChart` below pass their own, since "no runs" is not the right
   * explanation for a window that had runs but none of them recorded a comparison. */
  emptyLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="min-w-0 rounded-xl border border-border bg-card p-4">
      <h3 className="text-sm font-medium text-foreground">{title}</h3>
      <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>

      <div className="relative mt-3 min-w-0">
        <div aria-hidden={isEmpty || undefined} className={isEmpty ? "opacity-40" : undefined}>
          {children}
        </div>

        {isEmpty && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <span className="rounded-md border border-border bg-card/90 px-2.5 py-1 text-xs text-muted-foreground">
              {emptyLabel}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}

// Below `md` every chart keeps a fixed 16:9 box (`aspect-video`, the ChartContainer default);
// above it they switch to a fixed height instead. Both halves reserve their space before any
// data arrives, which is what stops the panel jumping when it lands — and the ratio is what
// keeps a 375px screen from getting a 90px-tall chart, which is unreadable in a way that a
// slightly-too-tall one is not.
const TIME_SERIES_BOX = "w-full md:aspect-auto md:h-56";

/** Hour ticks read "Wed 14:00" and day ticks read "Aug 5", so they need different amounts of
 * room before Recharts is allowed to place the next one. Passing the gap rather than a tick
 * count is what makes the axis thin itself out responsively instead of overlapping at 375px. */
function tickGapFor(bucket: StatsBucket): number {
  return bucket === "hour" ? 56 : 28;
}

// ---------------------------------------------------------------------------------------
// The three charts
// ---------------------------------------------------------------------------------------

interface TimeSeriesProps {
  points: TrendPoint[];
  bucket: StatsBucket;
  isEmpty: boolean;
}

/**
 * The generated `llms.txt` index's size per bucket, in KB — the last completed run in each
 * bucket that recorded one.
 *
 * A line, not bars: index size is a LEVEL — "how big is this site's index around this time" —
 * not a quantity accumulated within one bucket, the same distinction the deleted
 * `DurationChart` drew for run duration.
 *
 * **`connectNulls={false}`, and this is the one chart in this panel that needs it.** Contrast
 * the deleted `DurationChart`'s own comment ("no `connectNulls`, there are no nulls to
 * connect"): THAT chart's zero buckets were real zeroes with nothing to bridge. This chart's
 * `indexKb` is `null` — not `0` — for a bucket with no completed run to ask
 * (`toTrendPoints`'s docstring, lib/crawls/stats-display.ts), and Recharts must break the line
 * across that gap rather than bridge it. Bridging it would draw a straight line through a
 * period this data cannot speak to, at whatever value happens to interpolate between the two
 * real points either side of it — exactly the kind of confident-looking lie `connectNulls`
 * exists to prevent.
 */
export function IndexSizeChart({ points, bucket, isEmpty }: TimeSeriesProps) {
  return (
    <ChartCard
      title="Index size"
      description="llms.txt size per bucket, from the last completed run in it."
      isEmpty={isEmpty}
      emptyLabel="No comparisons in this window"
    >
      <ChartContainer config={INDEX_SIZE_CHART_CONFIG} className={TIME_SERIES_BOX}>
        <LineChart accessibilityLayer data={points} margin={{ left: 4, right: 8, top: 4 }}>
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="tick"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            minTickGap={tickGapFor(bucket)}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            width={48}
            tickMargin={4}
            tickFormatter={(value: number) => `${value} KB`}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                labelFormatter={bucketTooltipLabel}
                formatter={(value, _name, item) => {
                  const point = trendPointOf(item?.payload);
                  const kb = typeof value === "number" ? `${value.toFixed(1)} KB` : String(value);
                  const pages =
                    point?.indexPages === null || point?.indexPages === undefined
                      ? ""
                      : ` · ${point.indexPages.toLocaleString()} pages`;
                  return (
                    <span className="font-mono text-foreground tabular-nums">
                      {kb}
                      {pages}
                    </span>
                  );
                }}
              />
            }
          />
          <Line
            dataKey="indexKb"
            // `linear`, not `monotone`, for the same reason the deleted `DurationChart` chose
            // it: these are discrete per-bucket measurements with nothing between them, and a
            // spline would round the approach into a gap in a way that misrepresents it as a
            // gentle trend rather than an absence of data.
            type="linear"
            stroke="var(--color-indexKb)"
            strokeWidth={2}
            // A visible dot on every point, unlike the deleted `DurationChart`'s `dot={false}`
            // (168 hourly circles would be noise) — and this is not the same situation.
            // `connectNulls={false}` means an isolated real point, with `null` on both
            // neighbouring buckets, has no line segment to appear as: with `dot={false}` it
            // would render NOTHING at all, silently dropping the one measurement in the
            // window. A small, low-opacity dot keeps every real point visible on its own,
            // whether or not it happens to sit next to another one.
            dot={{ r: 2.5, strokeWidth: 0, fill: "var(--color-indexKb)" }}
            activeDot={{ r: 4 }}
            connectNulls={false}
            isAnimationActive={ANIMATE}
          />
        </LineChart>
      </ChartContainer>
    </ChartCard>
  );
}

/**
 * Pages added and removed per bucket, as a diverging bar — added reaching up from zero,
 * removed reaching down from it, on a shared `stackId` so the two never overlap and a
 * `ReferenceLine` at `y={0}` marks the axis both series share.
 *
 * Bars, not a line: `pages_added`/`pages_removed` are quantities accumulated WITHIN one
 * bucket (how many pages changed around this time), the same shape the deleted `PagesChart`
 * chose bars for, not a level a line would suit.
 *
 * No `connectNulls` here either, but for the opposite reason `IndexSizeChart` needs one:
 * `added`/`removedNegative` are never `null` — a bucket with no comparisons measured zero of
 * each, which is a real number a bar renders as no bar at all, not a gap to bridge.
 */
export function ChangeChart({ points, bucket, isEmpty }: TimeSeriesProps) {
  return (
    <ChartCard
      title="Index changes"
      description="Pages added and removed per bucket, from runs that recorded a comparison."
      isEmpty={isEmpty}
      emptyLabel="No comparisons in this window"
    >
      <ChartContainer config={CHANGE_CHART_CONFIG} className={TIME_SERIES_BOX}>
        <BarChart accessibilityLayer data={points} margin={{ left: 4, right: 8, top: 4 }}>
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="tick"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            minTickGap={tickGapFor(bucket)}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            width={36}
            allowDecimals={false}
            tickMargin={4}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                labelFormatter={bucketTooltipLabel}
                formatter={(value) => (
                  <span className="font-mono text-foreground tabular-nums">
                    {signedCount(Number(value))}
                  </span>
                )}
              />
            }
          />
          <ReferenceLine y={0} stroke="var(--border)" />
          <Bar
            dataKey="added"
            stackId="change"
            fill="var(--color-added)"
            isAnimationActive={ANIMATE}
          />
          <Bar
            dataKey="removedNegative"
            stackId="change"
            fill="var(--color-removedNegative)"
            isAnimationActive={ANIMATE}
          />
        </BarChart>
      </ChartContainer>
    </ChartCard>
  );
}

/**
 * Completed vs failed vs still-running, as one stacked ratio bar.
 *
 * A bar and not a pie, deliberately: for two or three categories a pie asks the reader to
 * compare angles where a bar lets them compare lengths against a shared baseline, and the
 * comparison people actually make here — "how much of this bar is red" — is exactly what a
 * stacked bar answers at a glance.
 *
 * The counts are also printed underneath rather than left to the tooltip. A tooltip is
 * unreachable by keyboard and on touch, and "22 failed" is the number someone came to this
 * panel to read; hiding it behind a hover would make the chart decorative.
 */
export function OutcomeChart({
  breakdown,
  isEmpty,
}: {
  breakdown: OutcomeBreakdown;
  isEmpty: boolean;
}) {
  // A zero total would give the x-axis the degenerate domain [0, 0], which Recharts scales
  // into NaN bar widths. Clamping to 1 keeps the axis valid and every segment at zero width,
  // which draws the empty track the overlay then labels.
  const domainMax = Math.max(1, breakdown.total);

  return (
    <ChartCard
      title="Outcomes"
      description="How runs in this window ended."
      isEmpty={isEmpty}
    >
      <ChartContainer config={OUTCOME_CHART_CONFIG} className="aspect-auto h-12 w-full">
        <BarChart
          accessibilityLayer
          layout="vertical"
          data={[breakdown]}
          margin={{ left: 0, right: 0, top: 0, bottom: 0 }}
        >
          <XAxis type="number" domain={[0, domainMax]} hide />
          <YAxis type="category" dataKey="total" hide />
          <ChartTooltip content={<ChartTooltipContent hideLabel />} />
          <Bar
            dataKey="completed"
            stackId="outcomes"
            fill="var(--color-completed)"
            isAnimationActive={ANIMATE}
          />
          <Bar
            dataKey="failed"
            stackId="outcomes"
            fill="var(--color-failed)"
            isAnimationActive={ANIMATE}
          />
          <Bar
            dataKey="inProgress"
            stackId="outcomes"
            fill="var(--color-inProgress)"
            isAnimationActive={ANIMATE}
          />
        </BarChart>
      </ChartContainer>

      <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="size-2 shrink-0 rounded-full bg-status-completed"
          />
          <span className="tabular-nums">{breakdown.completed.toLocaleString()} completed</span>
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="size-2 shrink-0 rounded-full bg-status-failed" />
          <span className="tabular-nums">{breakdown.failed.toLocaleString()} failed</span>
        </span>
        {/* Only shown when there is one. A permanent "0 in progress" on every website that
            is not mid-crawl is noise on the common case. */}
        {breakdown.inProgress > 0 && (
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-full bg-status-processing"
            />
            <span className="tabular-nums">
              {breakdown.inProgress.toLocaleString()} in progress
            </span>
          </span>
        )}
      </p>
    </ChartCard>
  );
}

/**
 * The charts' loading state — the same three cards at the same three heights, so the panel
 * does not resize when the series arrive.
 */
export function TrendsChartsSkeleton() {
  return (
    <div aria-hidden="true" className="space-y-4">
      {[TIME_SERIES_BOX, TIME_SERIES_BOX, "h-12 w-full"].map((box, index) => (
        <div key={index} className="rounded-xl border border-border bg-card p-4">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="mt-2 h-3 w-44" />
          <Skeleton className={`mt-3 ${index === 2 ? "" : "aspect-video "}${box}`} />
        </div>
      ))}
    </div>
  );
}
