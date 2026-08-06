"use client";

import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { WebsiteListItem } from "@/lib/api/websites";
import { rowActivationProps, useOpenCrawl } from "@/lib/crawls/row-activation";
import { lastActivityAt, pagesCrawled, rowStatus, rowStatusToken } from "@/lib/crawls/row-status";
import type { SortKey, SortState } from "@/lib/crawls/sort";
import { cn } from "@/lib/utils";

import { COLUMN_WIDTH, ROW_HEIGHT } from "./columns";
import { CrawlOwner } from "./crawl-owner";
import { CrawlSchedule } from "./crawl-schedule";
import { CrawlSite } from "./crawl-site";
import { EmptyCell } from "./empty-cell";
import { RelativeTime } from "./relative-time";
import { HIGHLIGHT_SURFACE_CLASS, RunStatusIndicator } from "./run-status-indicator";
import { PlainHeader, SortHeader } from "./sort-header";

interface CrawlsTableProps {
  websites: WebsiteListItem[];
  currentUserId: string | null;
  sort: SortState;
  onSort: (key: SortKey) => void;
  /** Ids of rows whose status just changed — see lib/crawls/use-status-change-highlight.ts. */
  highlighted: ReadonlySet<string>;
}

/**
 * The desktop table. Six columns, one row per website, the whole row clickable.
 *
 * Hidden below `md` by its parent (crawls-view.tsx), which renders a card list instead. That
 * is a CSS decision, not a JavaScript one — two markup trees behind Tailwind breakpoints,
 * never a `window.innerWidth` check. A width measured in JavaScript is not available during
 * server rendering, so branching on one means the server picks a layout, the browser picks
 * possibly the other, and React reconciles the difference as a hydration error.
 *
 * `table-fixed` plus the shared widths in columns.ts are what let a long page title ellipse
 * rather than widening the table past its container.
 */
export function CrawlsTable({
  websites,
  currentUserId,
  sort,
  onSort,
  highlighted,
}: CrawlsTableProps) {
  const openCrawl = useOpenCrawl();

  return (
    <Table className="table-fixed">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <SortHeader label="Site" sortKey="site" state={sort} onSort={onSort} className={COLUMN_WIDTH.site} />
          <SortHeader
            label="Status"
            sortKey="status"
            state={sort}
            onSort={onSort}
            className={COLUMN_WIDTH.status}
          />
          <PlainHeader label="Pages" className={cn(COLUMN_WIDTH.pages, "text-right")} />
          <SortHeader
            label="Last run"
            sortKey="lastRun"
            state={sort}
            onSort={onSort}
            className={COLUMN_WIDTH.lastRun}
          />
          <PlainHeader label="Schedule" className={COLUMN_WIDTH.schedule} />
          <PlainHeader label="Owner" className={COLUMN_WIDTH.owner} />
        </TableRow>
      </TableHeader>

      <TableBody>
        {websites.map((website) => {
          const status = rowStatus(website);
          const pages = pagesCrawled(website);
          const lastRunAt = lastActivityAt(website);
          const isHighlighted = highlighted.has(website.id);

          return (
            <TableRow
              key={website.id}
              {...rowActivationProps(openCrawl, website.id)}
              className={cn(
                ROW_HEIGHT,
                "cursor-pointer duration-700 outline-none",
                // `outline`, not `ring`: Tailwind's ring utilities are box-shadows, and a
                // box-shadow on a `<tr>` does not paint under `border-collapse: collapse`,
                // which is what Tailwind's preflight sets on every table. The focus
                // indicator would silently not exist.
                //
                // `outline-solid` is not redundant next to `outline-2`. In Tailwind v4,
                // `outline-none` above sets `--tw-outline-style: none`, and `outline-2` sets
                // only the *width*, taking its style from that same variable — so the two
                // together produce a 2px outline with `outline-style: none`, which paints
                // nothing. Measured in Chrome, not reasoned about: the focus ring was
                // genuinely invisible before this class was added, and nothing else in the
                // toolchain notices.
                "focus-visible:outline-solid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
                isHighlighted && HIGHLIGHT_SURFACE_CLASS[rowStatusToken(status)]
              )}
            >
              <TableCell className="px-3">
                <CrawlSite website={website} />
              </TableCell>

              <TableCell className="px-3">
                <RunStatusIndicator status={status} />
              </TableCell>

              <TableCell className="px-3 text-right text-sm tabular-nums text-foreground">
                {pages === null ? <EmptyCell label="no pages counted" /> : pages}
              </TableCell>

              <TableCell className="px-3 text-sm text-muted-foreground">
                {lastRunAt === null ? "Never" : <RelativeTime iso={lastRunAt} />}
              </TableCell>

              <TableCell className="px-3">
                <CrawlSchedule website={website} />
              </TableCell>

              <TableCell className="px-3">
                <CrawlOwner userId={website.user_id} currentUserId={currentUserId} />
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
