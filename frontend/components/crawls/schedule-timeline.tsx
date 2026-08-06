"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { RunListItem } from "@/lib/api/runs";
import { formatAbsoluteTime, formatCountdown } from "@/lib/crawls/relative-time";
import { rowStatusFromRunStatus } from "@/lib/crawls/row-status";
import { jitterWindow, SCHEDULE_JITTER_FRACTION } from "@/lib/crawls/schedule-presets";
import { useNow } from "@/lib/crawls/use-now";

import { RelativeTime } from "./relative-time";
import { RunStatusIndicator } from "./run-status-indicator";

/** One `Label   value` line. A two-column grid rather than a flex row so "Next run" and
 * "Last run" line their values up with each other, which is the only reason these two rows
 * read as a pair. */
function TimelineRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[6rem_minmax(0,1fr)] items-baseline gap-x-4 gap-y-1">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="min-w-0 text-sm text-foreground">{children}</span>
    </div>
  );
}

function Separator() {
  return (
    <span aria-hidden="true" className="px-1.5 text-muted-foreground/40">
      ·
    </span>
  );
}

/**
 * When the next run is due, and how the last one went.
 *
 * ## Both halves of "when", on one line
 *
 * "in 4h 12m" alone makes the reader do arithmetic to answer "so, tonight?", and
 * "Aug 5, 2026 at 02:00" alone makes them do it in the other direction. The countdown ticks
 * off the shared page clock (lib/crawls/use-now.ts — one timer for the whole page, not one
 * per component), so it never sits frozen at the value it had when the tab was opened.
 *
 * The absolute half is rendered by `formatAbsoluteTime`, whose `Intl.DateTimeFormat` is
 * constructed with an undefined locale and therefore formats in the *viewer's* timezone.
 * Schedules are stored and computed in UTC; nothing here converts anything, and nothing here
 * should start offering a timezone picker.
 *
 * ## The jitter note is not decoration
 *
 * PER-164 spreads `next_run_at` by up to ±5% so that every daily schedule in the system does
 * not fire in the same minute. The visible consequence is a run landing at 02:11 when the
 * panel said 02:00, which looks exactly like a bug to someone who has not been told. One
 * tooltip is cheaper than that support conversation, and it states the actual window in
 * minutes rather than making the reader compute 5% of their interval.
 */
export function ScheduleTimeline({
  active,
  nextRunAt,
  intervalMinutes,
  latestRun,
}: {
  active: boolean;
  nextRunAt: string | null;
  /** The interval currently shown by the picker, used only to state the jitter window in
   * real minutes. `null` when the stored interval is not one of the presets. */
  intervalMinutes: number | null;
  /** The newest run for this website, from the query `WebsiteDetail` already hoists for the
   * whole page. `null` when the site has never run. */
  latestRun: RunListItem | null;
}) {
  const now = useNow();

  return (
    <div className="space-y-3">
      <TimelineRow label="Next run">
        {!active ? (
          // The ticket is specific about this wording: not a stale timestamp left over from
          // when the schedule was on, and not an em dash. A paused schedule has no next run,
          // and saying so is the whole message.
          <span className="text-muted-foreground">Not scheduled</span>
        ) : nextRunAt === null ? (
          <span className="text-muted-foreground">
            Scheduled — the next run time has not been calculated yet
          </span>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <time dateTime={nextRunAt} className="cursor-default tabular-nums">
                {formatCountdown(nextRunAt, now)}
                <Separator />
                <span className="text-muted-foreground">{formatAbsoluteTime(nextRunAt)}</span>
                {/* The visible half of the jitter note. A bare "±" next to the time is
                    enough to signal "approximate" to someone skimming; the tooltip is for
                    whoever stops to ask why. */}
                <span aria-hidden="true" className="pl-1.5 text-muted-foreground/60">
                  ±
                </span>
              </time>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              Runs are spread out by up to ±
              {Math.round(SCHEDULE_JITTER_FRACTION * 100)}% so that everything due at the same
              moment does not start at once
              {intervalMinutes === null
                ? ""
                : `, so this one may fire up to ${jitterWindow(intervalMinutes)} either side of the time shown`}
              .
            </TooltipContent>
          </Tooltip>
        )}
      </TimelineRow>

      <TimelineRow label="Last run">
        {latestRun === null ? (
          <span className="text-muted-foreground">Never run</span>
        ) : (
          <span className="inline-flex flex-wrap items-center">
            {/* `completed_at` when the run has finished, `started_at` while it has not —
                "last run 2 minutes ago" should mean the run itself, and a run still in
                flight has no completion to date from. */}
            <RelativeTime iso={latestRun.completed_at ?? latestRun.started_at} />
            <Separator />
            <RunStatusIndicator status={rowStatusFromRunStatus(latestRun.status)} />
          </span>
        )}
      </TimelineRow>
    </div>
  );
}
