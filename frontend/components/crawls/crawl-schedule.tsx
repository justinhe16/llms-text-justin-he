"use client";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { WebsiteListItem } from "@/lib/api/websites";
import { formatAbsoluteTime } from "@/lib/crawls/relative-time";
import { scheduleLabel } from "@/lib/crawls/row-status";

import { EmptyCell } from "./empty-cell";

/**
 * The Schedule column: an interval badge when the website crawls itself on a timer, an em
 * dash when it does not.
 *
 * A schedule that exists but is switched off renders as no schedule here (see
 * `scheduleLabel`) — from this table's point of view the row is not going to run on its own
 * either way, and "paused" versus "never configured" is a distinction the detail page can
 * afford to make and a six-column overview cannot.
 *
 * When the backend knows when the next run is due, that goes in a tooltip rather than a
 * second line: it is the natural follow-up question to "daily", and it is not worth a column
 * of its own.
 */
export function CrawlSchedule({ website }: { website: WebsiteListItem }) {
  const label = scheduleLabel(website);
  if (label === null) return <EmptyCell label="no schedule" />;

  const nextRunAt = website.schedule?.next_run_at ?? null;
  const badge = (
    <Badge variant="outline" className="font-normal text-muted-foreground">
      {label}
    </Badge>
  );

  if (nextRunAt === null) return badge;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex cursor-default">{badge}</span>
      </TooltipTrigger>
      <TooltipContent>Next run {formatAbsoluteTime(nextRunAt)}</TooltipContent>
    </Tooltip>
  );
}
