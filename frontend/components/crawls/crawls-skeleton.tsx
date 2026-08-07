"use client";

import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { DEFAULT_SORT } from "@/lib/crawls/sort";
import { cn } from "@/lib/utils";

import { COLUMN_WIDTH, ROW_HEIGHT, SKELETON_ROW_COUNT } from "./columns";
import { PlainHeader, SortHeader } from "./sort-header";

const ROW_INDEXES = Array.from({ length: SKELETON_ROW_COUNT }, (_, index) => index);

// Varying the site-name width down the column stops the placeholder reading as a solid
// block and makes it obvious at a glance that this is a list of different things loading,
// not one thing.
const SITE_WIDTHS = ["w-32", "w-40", "w-28", "w-36", "w-24"];

/** No-op: the skeleton renders the *real* header so the column widths and the header row's
 * height are identical to the loaded table, but there is nothing yet to sort. */
function ignoreSort() {}

/**
 * The loading state: the table's actual shape, with placeholder content in it.
 *
 * A centred spinner would guarantee the thing this avoids — the page is one height while
 * loading and a different height a moment later, so everything below it jumps. These rows
 * are the same height as real rows and sit in the same columns (columns.ts holds both, for
 * exactly this reason), so when the data arrives the only thing that changes is the text.
 *
 * The whole visual is `aria-hidden`, with a single polite status message behind it: a screen
 * reader should hear "loading", not five rows of nothing.
 */
export function CrawlsSkeleton() {
  return (
    <>
      <span role="status" className="sr-only">
        Loading crawls…
      </span>

      <div aria-hidden="true" className="pointer-events-none">
        {/* Desktop */}
        <div className="hidden md:block">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <SortHeader
                  label="Site"
                  sortKey="site"
                  state={DEFAULT_SORT}
                  onSort={ignoreSort}
                  className={COLUMN_WIDTH.site}
                />
                <SortHeader
                  label="Status"
                  sortKey="status"
                  state={DEFAULT_SORT}
                  onSort={ignoreSort}
                  className={COLUMN_WIDTH.status}
                />
                <PlainHeader label="Pages" className={cn(COLUMN_WIDTH.pages, "text-right")} />
                <SortHeader
                  label="Last run"
                  sortKey="lastRun"
                  state={DEFAULT_SORT}
                  onSort={ignoreSort}
                  className={COLUMN_WIDTH.lastRun}
                />
                <PlainHeader label="Schedule" className={COLUMN_WIDTH.schedule} />
                <PlainHeader label="Owner" className={COLUMN_WIDTH.owner} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {ROW_INDEXES.map((index) => (
                <TableRow key={index} className={cn(ROW_HEIGHT, "hover:bg-transparent")}>
                  <TableCell className="px-3">
                    <Skeleton className={cn("h-4", SITE_WIDTHS[index % SITE_WIDTHS.length])} />
                  </TableCell>
                  <TableCell className="px-3">
                    <div className="flex items-center gap-2">
                      <Skeleton className="size-2 rounded-full" />
                      <Skeleton className="h-4 w-16" />
                    </div>
                  </TableCell>
                  <TableCell className="px-3">
                    <Skeleton className="ml-auto h-4 w-6" />
                  </TableCell>
                  <TableCell className="px-3">
                    <Skeleton className="h-4 w-14" />
                  </TableCell>
                  <TableCell className="px-3">
                    <Skeleton className="h-5 w-12 rounded-4xl" />
                  </TableCell>
                  <TableCell className="px-3">
                    <Skeleton className="h-5 w-20 rounded-4xl" />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        {/* Below md — the card list's shape, for the same reason. */}
        <ul className="flex flex-col gap-2 md:hidden">
          {ROW_INDEXES.map((index) => (
            <li
              key={index}
              className="flex flex-col gap-3 rounded-lg border border-border bg-card p-3"
            >
              <Skeleton className={cn("h-4", SITE_WIDTHS[index % SITE_WIDTHS.length])} />
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Skeleton className="size-2 rounded-full" />
                  <Skeleton className="h-4 w-16" />
                </div>
                <Skeleton className="h-4 w-12" />
              </div>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
