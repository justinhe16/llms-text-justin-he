"use client";

import { useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { RunListItem } from "@/lib/api/runs";
import { formatAbsoluteTime, formatDuration, formatRelativeTime } from "@/lib/format/time";
import { runPagesCrawled } from "@/lib/format/run";
import { cn } from "@/lib/utils";

import { RunStatusBadge } from "./run-status-indicator";

/** The five columns, in order. `numeric` right-aligns a column and puts it in the tabular
 * figures the mono face provides, so page counts and durations line up on their digits. */
const COLUMNS = [
  { key: "started", label: "Started", numeric: false },
  { key: "trigger", label: "Trigger", numeric: false },
  { key: "status", label: "Status", numeric: false },
  { key: "pages", label: "Pages", numeric: true },
  { key: "duration", label: "Duration", numeric: true },
] as const;

type RunsTabProps = {
  runs: RunListItem[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
  /** Row click — switches to the Output tab with this run selected. */
  onSelectRun: (runId: string) => void;
  /** Whether the current user may trigger a run, so the empty state can point at the right
   * thing instead of at a button the reader cannot press. */
  canRun: boolean;
};

/**
 * A website's run history.
 *
 * ## Two different clicks on one row
 *
 * The row itself opens the run in the Output tab. The chevron on a failed row expands its
 * error inline. Those are different intentions, so they are different targets: the chevron
 * is a real `<button>` that stops propagation, rather than the row doing one thing in one
 * region and another elsewhere.
 *
 * Errors expand rather than always showing because they can be long — a stack trace or a
 * fetch failure with a full URL in it — and a permanently expanded one would break the
 * table's rhythm for every row that has none.
 */
export function RunsTab({
  runs,
  isLoading,
  isError,
  error,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  onSelectRun,
  canRun,
}: RunsTabProps) {
  // Which failed rows have their error open. A `Set` of ids, not an index or a single
  // "expandedId": several errors can be open at once, and an index would attach the
  // expansion to a *position* — which shifts under it the moment polling puts a new run at
  // the top of the list.
  const [expandedRunIds, setExpandedRunIds] = useState<ReadonlySet<string>>(new Set());

  const toggleExpanded = (runId: string) => {
    setExpandedRunIds((previous) => {
      const next = new Set(previous);
      if (!next.delete(runId)) next.add(runId);
      return next;
    });
  };

  if (isLoading) return <RunsTableSkeleton />;

  if (isError) {
    return (
      <p className="rounded-lg border border-border bg-card p-6 text-sm text-status-failed">
        {error?.message ?? "Could not load this website's run history."}
      </p>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-10 text-center">
        <p className="text-sm font-medium text-foreground">No runs yet</p>
        <p className="mt-1 text-sm text-muted-foreground">
          {canRun
            ? "Use Run now above to crawl this site and generate its llms.txt."
            : "Nothing has crawled this site yet. Its owner can start a run from this page."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              {COLUMNS.map((column) => (
                <TableHead
                  key={column.key}
                  className={cn(
                    "text-xs font-medium text-muted-foreground",
                    column.numeric && "text-right"
                  )}
                >
                  {column.label}
                </TableHead>
              ))}
              {/* The expander's column. Headed by a screen-reader-only label rather than an
                  empty `<th>`, which reads as an unnamed column. */}
              <TableHead className="w-8">
                <span className="sr-only">Error details</span>
              </TableHead>
            </TableRow>
          </TableHeader>

          <TableBody>
            {runs.map((run) => (
              <RunRow
                key={run.id}
                run={run}
                isExpanded={expandedRunIds.has(run.id)}
                onToggleExpanded={() => toggleExpanded(run.id)}
                onSelect={() => onSelectRun(run.id)}
              />
            ))}
          </TableBody>
        </Table>
      </div>

      {hasNextPage && (
        <div className="flex justify-center">
          {/* "Load more", not infinite scroll: users scan run history looking for a
              specific run, they do not browse it, and an infinite list makes the end of the
              history unreachable and the page's own footer unreachable with it. */}
          <Button variant="outline" onClick={onLoadMore} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}

function RunRow({
  run,
  isExpanded,
  onToggleExpanded,
  onSelect,
}: {
  run: RunListItem;
  isExpanded: boolean;
  onToggleExpanded: () => void;
  onSelect: () => void;
}) {
  // Only a failed run has an error to expand. A run that failed with no error text still
  // gets the expander — "failed, and the backend recorded no reason" is information, and
  // silently having no control on that row looks like a rendering bug.
  const isExpandable = run.status === "failed";
  const pages = runPagesCrawled(run);

  return (
    <>
      <TableRow
        // A row is not natively focusable or activatable, so the keyboard handling is
        // explicit. `role="button"` plus Enter/Space is the same contract a real button
        // offers, which is what a row that navigates should behave like.
        role="button"
        tabIndex={0}
        aria-label={`View the llms.txt from the run started ${formatAbsoluteTime(run.started_at)}`}
        onClick={onSelect}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          onSelect();
        }}
        className="cursor-pointer focus-visible:bg-muted/50 focus-visible:outline-none"
      >
        <TableCell>
          <Tooltip>
            <TooltipTrigger asChild>
              {/* `<time>` with a machine-readable `dateTime`, so the exact instant is in
                  the markup even though the text is relative. */}
              <time dateTime={run.started_at} className="text-sm">
                {formatRelativeTime(run.started_at)}
              </time>
            </TooltipTrigger>
            <TooltipContent>{formatAbsoluteTime(run.started_at)}</TooltipContent>
          </Tooltip>
        </TableCell>

        <TableCell>
          <span
            className={cn(
              "inline-flex items-center rounded-4xl border px-2 py-0.5 text-xs font-medium",
              run.trigger === "manual"
                ? "border-border text-foreground"
                : "border-transparent bg-secondary text-muted-foreground"
            )}
          >
            {run.trigger === "manual" ? "Manual" : "Scheduled"}
          </span>
        </TableCell>

        <TableCell>
          <RunStatusBadge status={run.status} />
        </TableCell>

        {/* Tabular figures: without them the mono column still jitters between rows,
            because the sans face's digits are proportional. */}
        <TableCell className="text-right font-mono text-xs tabular-nums">
          {pages === null ? "—" : pages.toLocaleString()}
        </TableCell>

        <TableCell className="text-right font-mono text-xs tabular-nums">
          {formatDuration(run.duration_ms)}
        </TableCell>

        <TableCell className="w-8">
          {isExpandable && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-expanded={isExpanded}
              aria-label={isExpanded ? "Hide the error" : "Show the error"}
              onClick={(event) => {
                // Without this the row's own handler also fires and navigates to the
                // Output tab, so expanding an error would always leave the Runs tab.
                event.stopPropagation();
                onToggleExpanded();
              }}
            >
              {isExpanded ? <ChevronDownIcon aria-hidden /> : <ChevronRightIcon aria-hidden />}
            </Button>
          )}
        </TableCell>
      </TableRow>

      {isExpandable && isExpanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={COLUMNS.length + 1} className="p-0">
            {/* `whitespace-pre-wrap` + `break-words`: an error is arbitrary text that may
                contain newlines and may contain a single unbroken 400-character URL. The
                first preserves the structure, the second stops the second case from
                widening the table and, through it, the page. */}
            <pre className="max-h-64 overflow-auto border-t border-border bg-status-failed-surface px-4 py-3 font-mono text-xs whitespace-pre-wrap break-words text-status-failed">
              {run.error ?? "This run failed, but recorded no error message."}
            </pre>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function RunsTableSkeleton() {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-card p-4">
      {Array.from({ length: 5 }, (_, index) => (
        <Skeleton key={index} className="h-9 w-full" />
      ))}
    </div>
  );
}
