"use client";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatAbsoluteTime, formatRelativeTime } from "@/lib/crawls/relative-time";
import { useNow } from "@/lib/crawls/use-now";
import { cn } from "@/lib/utils";

interface RelativeTimeProps {
  /** An ISO-8601 timestamp from the API. */
  iso: string;
  className?: string;
}

/**
 * "2m ago", with the full timestamp in a tooltip.
 *
 * Re-renders on the shared clock (lib/crawls/use-now.ts) rather than being formatted once,
 * so a row does not sit reading "2m ago" for twenty minutes. That is one timer for the whole
 * page, not one per cell.
 *
 * The trigger is a `<time>` element, not a button, for two reasons. It is the correct
 * element — `dateTime` carries the machine-readable timestamp, so the absolute value reaches
 * a screen reader whether or not the tooltip is ever opened. And it is not focusable, which
 * keeps the row containing it to exactly one tab stop: the row itself
 * (lib/crawls/row-activation.ts). A focusable tooltip trigger in every Last-run cell would
 * double the number of tab presses it takes to get down this table, to reveal information
 * `dateTime` already provides.
 */
export function RelativeTime({ iso, className }: RelativeTimeProps) {
  const now = useNow();

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <time
          dateTime={iso}
          className={cn("cursor-default tabular-nums", className)}
        >
          {formatRelativeTime(iso, now)}
        </time>
      </TooltipTrigger>
      <TooltipContent>{formatAbsoluteTime(iso)}</TooltipContent>
    </Tooltip>
  );
}
