"use client";

import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import type { StatsBucket } from "@/lib/api/runs";
import type { OutcomeBreakdown, TrendPoint } from "@/lib/crawls/stats-display";

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

const PAGES_CHART_CONFIG = {
  pages: { label: "Avg pages per run", color: "var(--chart-1)" },
} satisfies ChartConfig;

const DURATION_CHART_CONFIG = {
  seconds: { label: "Avg duration", color: "var(--chart-2)" },
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
  children,
}: {
  title: string;
  description: string;
  isEmpty: boolean;
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
              No runs in this window
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
 * Pages crawled per bucket.
 *
 * Bars rather than a line or an area, specifically because of the zero buckets. The API
 * zero-fills every quiet hour, and a bar chart renders "nothing ran here" as the absence of a
 * bar — which is what happened — where a line has to travel down to the axis and back up,
 * drawing two slopes through a period in which nothing changed because nothing occurred.
 */
export function PagesChart({ points, bucket, isEmpty }: TimeSeriesProps) {
  return (
    <ChartCard
      title="Pages crawled"
      description="Average pages per run, per bucket."
      isEmpty={isEmpty}
    >
      <ChartContainer config={PAGES_CHART_CONFIG} className={TIME_SERIES_BOX}>
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
          <ChartTooltip content={<ChartTooltipContent labelFormatter={bucketTooltipLabel} />} />
          <Bar dataKey="pages" fill="var(--color-pages)" isAnimationActive={ANIMATE} />
        </BarChart>
      </ChartContainer>
    </ChartCard>
  );
}

/**
 * Run duration per bucket, in seconds.
 *
 * A line here, where pages got bars, because duration is a level rather than a quantity
 * accumulated in each bucket — "how long a run took around this time" is a thing that trends,
 * and the eye reads a trend off a line far better than off a row of columns.
 *
 * **No `connectNulls`, and no gap handling of any kind.** There are no nulls to connect: a
 * bucket with no runs reports `0`, and it is drawn as `0`. Bridging those points would draw a
 * straight line across an outage at the average of its two ends, which is the single most
 * confident-looking way this chart could lie.
 */
export function DurationChart({ points, bucket, isEmpty }: TimeSeriesProps) {
  return (
    <ChartCard
      title="Run duration"
      description="Average seconds per run, per bucket."
      isEmpty={isEmpty}
    >
      <ChartContainer config={DURATION_CHART_CONFIG} className={TIME_SERIES_BOX}>
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
            width={40}
            tickMargin={4}
            tickFormatter={(value: number) => `${value}s`}
          />
          <ChartTooltip
            content={
              <ChartTooltipContent
                labelFormatter={bucketTooltipLabel}
                formatter={(value) => (
                  <span className="font-mono text-foreground tabular-nums">
                    {typeof value === "number" ? `${value.toFixed(1)}s` : String(value)}
                  </span>
                )}
              />
            }
          />
          <Line
            dataKey="seconds"
            // `linear`, not `monotone`. A monotone spline still passes through every real
            // point, but it rounds the approach into and out of a zero bucket, so a day with
            // no runs reads as a gentle dip rather than a drop to the axis. These are
            // discrete per-bucket measurements with nothing between them; straight segments
            // are the honest join, and they keep an outage looking like an outage.
            type="linear"
            stroke="var(--color-seconds)"
            strokeWidth={2}
            // No dot per point: 168 hourly buckets would be 168 circles, which reads as
            // noise. The active dot on hover still marks whichever bucket is being read.
            dot={false}
            isAnimationActive={ANIMATE}
          />
        </LineChart>
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
