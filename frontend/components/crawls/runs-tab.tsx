"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

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
import type { RunListItem } from "@/lib/api/runs";
import { rowActivationProps } from "@/lib/crawls/row-activation";
import { rowStatusFromRunStatus } from "@/lib/crawls/row-status";
import { formatDuration, runPagesCrawled } from "@/lib/crawls/run-display";
import { cn } from "@/lib/utils";

import { CrawlsError } from "./crawls-error";
import { EmptyCell } from "./empty-cell";
import { RelativeTime } from "./relative-time";
import { RunStatusIndicator } from "./run-status-indicator";

/** The five columns, in order. `numeric` right-aligns a column and renders it in tabular
 * figures, so page counts and durations line up on their digits down the table. */
const COLUMNS = [
  { key: "started", label: "Started", numeric: false },
  { key: "trigger", label: "Trigger", numeric: false },
  { key: "status", label: "Status", numeric: false },
  { key: "pages", label: "Pages", numeric: true },
  { key: "duration", label: "Duration", numeric: true },
] as const;

const SKELETON_ROW_COUNT = 5;

interface RunsTabProps {
  runs: RunListItem[];
  isLoading: boolean;
  error: Error | null;
  onRetry: () => void;
  isRetrying: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
  /** Row activation — switches to the Output tab with this run selected. */
  onSelectRun: (runId: string) => void;
  /** Whether the reader may trigger a run, so the empty state points at something they can
   * actually do rather than at a button that is disabled for them. */
  canRun: boolean;
}

/**
 * A website's run history.
 *
 * ## Two different clicks on one row
 *
 * The row itself opens that run in the Output tab. The chevron on a failed row expands its
 * error in place. Those are different intentions, so they are different targets — and the
 * separation is enforced by `rowActivationProps` (lib/crawls/row-activation.ts, shared with
 * the /crawls table), whose interactive-child guard is exactly what stops a click on the
 * expander from also navigating.
 *
 * Errors expand rather than showing permanently because they can be long — a stack trace, or
 * a fetch failure with a full URL in it — and one always-visible error would break the
 * table's rhythm for every row that has none.
 */
export function RunsTab({
  runs,
  isLoading,
  error,
  onRetry,
  isRetrying,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  onSelectRun,
  canRun,
}: RunsTabProps) {
  // Which failed rows have their error open. A `Set` of run ids, not an index or a single
  // "expandedId": several can be open at once, and an index would tie the expansion to a
  // *position*, which shifts under it the moment polling puts a new run at the top.
  const [expandedRunIds, setExpandedRunIds] = useState<ReadonlySet<string>>(new Set());

  const toggleExpanded = (runId: string) => {
    setExpandedRunIds((previous) => {
      const next = new Set(previous);
      if (!next.delete(runId)) next.add(runId);
      return next;
    });
  };

  // Ordered so a failed refetch never blanks a table that is already readable: with rows on
  // screen the error renders as a strip above them (`hasStaleData`), and only a failure with
  // nothing to show takes over the panel. `CrawlsError` is shared with the /crawls table and
  // already draws that distinction.
  if (error !== null) {
    return (
      <div className="space-y-4">
        <CrawlsError
          error={error}
          onRetry={onRetry}
          isRetrying={isRetrying}
          hasStaleData={runs.length > 0}
        />
        {runs.length > 0 && (
          <RunsTable
            runs={runs}
            expandedRunIds={expandedRunIds}
            onToggleExpanded={toggleExpanded}
            onSelectRun={onSelectRun}
          />
        )}
      </div>
    );
  }

  if (isLoading) return <RunsSkeleton />;

  if (runs.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border border-border bg-card px-6 py-16 text-center">
        <p className="text-sm font-medium text-foreground">No runs yet</p>
        <p className="max-w-sm text-sm text-muted-foreground">
          {canRun
            ? "Use Run now above to crawl this site and generate its llms.txt."
            : "Nothing has crawled this site yet. Its owner can start a run from this page."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <RunsTable
        runs={runs}
        expandedRunIds={expandedRunIds}
        onToggleExpanded={toggleExpanded}
        onSelectRun={onSelectRun}
      />

      {hasNextPage && (
        <div className="flex justify-center">
          {/* "Load more", not infinite scroll: people scan run history for a particular
              run rather than browsing it, and an endless list puts the oldest run — and
              anything below the table — permanently out of reach. */}
          <Button variant="outline" onClick={onLoadMore} disabled={isFetchingNextPage}>
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}

function RunsTable({
  runs,
  expandedRunIds,
  onToggleExpanded,
  onSelectRun,
}: {
  runs: RunListItem[];
  expandedRunIds: ReadonlySet<string>;
  onToggleExpanded: (runId: string) => void;
  onSelectRun: (runId: string) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
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
            {/* The expander's column. Named for screen readers rather than left as an
                empty `<th>`, which announces as an unlabelled column. */}
            <TableHead className="w-10">
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
              onToggleExpanded={() => onToggleExpanded(run.id)}
              onSelectRun={onSelectRun}
            />
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function RunRow({
  run,
  isExpanded,
  onToggleExpanded,
  onSelectRun,
}: {
  run: RunListItem;
  isExpanded: boolean;
  onToggleExpanded: () => void;
  onSelectRun: (runId: string) => void;
}) {
  // Only a failed run has an error to expand. A failed run with no recorded error still gets
  // the control — "it failed and said nothing" is information, and a row that silently lacks
  // the affordance its neighbours have reads as a rendering bug.
  const isExpandable = run.status === "failed";
  const pages = runPagesCrawled(run);
  const duration = formatDuration(run.duration_ms);

  return (
    <>
      <TableRow
        {...rowActivationProps(onSelectRun, run.id)}
        aria-label="View the llms.txt from this run"
        className={cn(
          "cursor-pointer outline-none",
          // The same focus treatment `crawls-table.tsx` uses, for the same two reasons it
          // documents at length: `outline` rather than `ring`, because a ring is a
          // box-shadow and a box-shadow does not paint on a `<tr>` under the
          // `border-collapse: collapse` Tailwind's preflight sets; and `outline-solid`
          // explicitly, because `outline-none` above sets `--tw-outline-style: none` in
          // Tailwind v4 and `outline-2` sets only the width — the pair alone paints a 2px
          // outline with `outline-style: none`, i.e. nothing at all, and no static gate
          // notices. Verified in a real browser rather than reasoned about.
          "focus-visible:outline-solid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
        )}
      >
        <TableCell>
          <RelativeTime iso={run.started_at} className="text-sm" />
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
            {run.trigger === "manual" ? "manual" : "scheduled"}
          </span>
        </TableCell>

        <TableCell>
          <RunStatusIndicator status={rowStatusFromRunStatus(run.status)} />
        </TableCell>

        <TableCell className="text-right font-mono text-xs tabular-nums">
          {pages === null ? <EmptyCell label="no pages counted" /> : pages.toLocaleString()}
        </TableCell>

        <TableCell className="text-right font-mono text-xs tabular-nums">
          {duration === null ? <EmptyCell label="still running" /> : duration}
        </TableCell>

        <TableCell className="w-10">
          {isExpandable && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-expanded={isExpanded}
              aria-label={isExpanded ? "Hide the error" : "Show the error"}
              onClick={onToggleExpanded}
            >
              {isExpanded ? (
                <ChevronDown aria-hidden="true" />
              ) : (
                <ChevronRight aria-hidden="true" />
              )}
            </Button>
          )}
        </TableCell>
      </TableRow>

      {isExpandable && isExpanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={COLUMNS.length + 1} className="p-0">
            {/* `whitespace-pre-wrap` with `break-words`: an error is arbitrary text that may
                carry newlines and may carry one unbroken 400-character URL. The first keeps
                the structure, the second stops the second case widening the table — and
                through it, the page. */}
            <pre className="max-h-64 overflow-auto border-t border-border bg-status-failed-surface px-4 py-3 font-mono text-xs break-words whitespace-pre-wrap text-status-failed">
              {run.error ?? "This run failed, but recorded no error message."}
            </pre>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

/**
 * The loading state, shaped like the table it replaces so the panel does not change height
 * when the data lands. `aria-hidden` over the placeholder with one polite message behind it:
 * a screen reader should hear "loading", not five rows of nothing.
 */
function RunsSkeleton() {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div aria-hidden="true" className="space-y-3">
        {Array.from({ length: SKELETON_ROW_COUNT }, (_, index) => (
          <Skeleton key={index} className="h-8 w-full" />
        ))}
      </div>
      <span role="status" aria-live="polite" className="sr-only">
        Loading run history
      </span>
    </div>
  );
}
