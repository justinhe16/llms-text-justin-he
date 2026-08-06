"use client";

import type { WebsiteListItem } from "@/lib/api/websites";
import { rowActivationProps, useOpenCrawl } from "@/lib/crawls/row-activation";
import { lastActivityAt, rowStatus, rowStatusToken } from "@/lib/crawls/row-status";
import { cn } from "@/lib/utils";

import { CrawlSite } from "./crawl-site";
import { RelativeTime } from "./relative-time";
import { HIGHLIGHT_SURFACE_CLASS, RunStatusIndicator } from "./run-status-indicator";

interface CrawlsCardListProps {
  websites: WebsiteListItem[];
  highlighted: ReadonlySet<string>;
}

/**
 * The same data below `md`, as a list of cards.
 *
 * A six-column table does not fit on a 375px screen, and the two usual escapes are both
 * bad: shrink the type until it is unreadable, or let the table scroll sideways so half the
 * columns are permanently off-screen behind a gesture nobody discovers. This drops to the
 * three things worth knowing on a phone — which site, what state, how long ago — and leaves
 * Pages, Schedule and Owner to the detail page, which is one tap away.
 *
 * Sorting is not offered here. The list arrives in the order the table's default sort
 * produces (most recent activity first), which is the only ordering a phone-sized overview
 * needs; column headers to sort by are a desktop affordance and there is no header row to
 * hang them on.
 *
 * Each card carries the same activation props as a table row, so the keyboard behaviour is
 * identical: focusable, Enter or Space opens it.
 */
export function CrawlsCardList({ websites, highlighted }: CrawlsCardListProps) {
  const openCrawl = useOpenCrawl();

  return (
    <ul className="flex flex-col gap-2">
      {websites.map((website) => {
        const status = rowStatus(website);
        const lastRunAt = lastActivityAt(website);
        const isHighlighted = highlighted.has(website.id);

        return (
          <li
            key={website.id}
            {...rowActivationProps(openCrawl, website.id)}
            className={cn(
              "flex cursor-pointer flex-col gap-2 rounded-lg border border-border bg-card p-3 transition-colors duration-700 outline-none",
              "focus-visible:ring-3 focus-visible:ring-ring/50",
              isHighlighted && HIGHLIGHT_SURFACE_CLASS[rowStatusToken(status)]
            )}
          >
            <CrawlSite website={website} />
            <div className="flex items-center justify-between gap-3">
              <RunStatusIndicator status={status} />
              <span className="text-xs text-muted-foreground">
                {lastRunAt === null ? "Never" : <RelativeTime iso={lastRunAt} />}
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
