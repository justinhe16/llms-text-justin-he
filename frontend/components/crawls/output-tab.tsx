"use client";

import { ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { RunListItem } from "@/lib/api/runs";
import { isActiveRunStatus } from "@/lib/api/run-status";
import { useRun } from "@/lib/query/use-run";
import { rowStatusFromRunStatus } from "@/lib/crawls/row-status";
import { selectRunToShow } from "@/lib/crawls/select-run";

import { CrawlsError } from "./crawls-error";
import { LlmsTxtViewer } from "./llms-txt-viewer";
import { RelativeTime } from "./relative-time";
import { RunStatusIndicator } from "./run-status-indicator";

type OutputTabProps = {
  origin: string;
  runs: RunListItem[];
  isLoadingRuns: boolean;
  /** The run-history fetch's own failure, if it failed. Passed in — rather than this tab
   * treating an empty list as "no runs" — because the two are completely different things to
   * a reader: "this site has never been crawled" versus "we could not find out". */
  runsError: Error | null;
  onRetryRuns: () => void;
  isRetryingRuns: boolean;
  /** From `?run=` — `null` when the user has not picked one, in which case
   * `selectRunToShow` falls back to the most recent completed run. */
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
  canRun: boolean;
};

/**
 * Picks which run's `llms.txt` to show, and shows it.
 *
 * ## The default is the most recent *completed* run, not the most recent run
 *
 * The newest run is frequently `pending` — a user clicks "Run now" and lands here — and
 * defaulting to it would replace an artifact they can read with a spinner they cannot. The
 * fallback chain is: the run named in `?run=`, else the newest completed one, else the
 * newest run of any status (so a website whose only run failed shows that failure rather
 * than an empty page).
 *
 * ## One detail request, for one run
 *
 * `GET /runs/{id}` is the only endpoint that returns `llms_txt`, and it is issued from
 * `RunOutput` below — a component that is rendered once, for the selected run. That is the
 * performance trap this ticket calls out by name: the list endpoint omits `llms_txt`
 * precisely so a history of 200 runs does not pull 200 artifacts, and it would be undone by
 * calling `useRun` from inside a `.map` over the rows.
 */
export function OutputTab({
  origin,
  runs,
  isLoadingRuns,
  runsError,
  onRetryRuns,
  isRetryingRuns,
  selectedRunId,
  onSelectRun,
  canRun,
}: OutputTabProps) {
  const runIdToShow = selectRunToShow(runs, selectedRunId);

  if (isLoadingRuns) return <Skeleton className="h-64 w-full rounded-lg" />;

  // Before the empty state, never after it: with the history fetch failed, `runs` is `[]`
  // for a reason that has nothing to do with whether this site has ever been crawled, and
  // telling someone to "run a crawl" because we could not read the list is a lie the user
  // cannot debug.
  if (runsError !== null && runs.length === 0) {
    return (
      <CrawlsError
        error={runsError}
        onRetry={onRetryRuns}
        isRetrying={isRetryingRuns}
        hasStaleData={false}
      />
    );
  }

  // State 1 of 4: no runs at all.
  if (runIdToShow === null) {
    return (
      <div className="rounded-lg border border-border bg-card p-10 text-center">
        <p className="text-sm font-medium text-foreground">Run a crawl to generate llms.txt</p>
        <p className="mt-1 text-sm text-muted-foreground">
          {canRun
            ? "Use Run now above. The generated file will appear here."
            : "This site has never been crawled. Its owner can start a run from this page."}
        </p>
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-4">
      <RunPicker runs={runs} selectedRunId={runIdToShow} onSelectRun={onSelectRun} />
      <RunOutput runId={runIdToShow} origin={origin} />
    </div>
  );
}

/**
 * The dropdown that switches between runs, labelled by relative time and status — the two
 * things that distinguish one run from another to a person. A run id would be precise and
 * useless.
 */
function RunPicker({
  runs,
  selectedRunId,
  onSelectRun,
}: {
  runs: RunListItem[];
  selectedRunId: string;
  onSelectRun: (runId: string) => void;
}) {
  const selected = runs.find((run) => run.id === selectedRunId);

  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-muted-foreground">Showing</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" className="justify-between gap-2">
            <span className="flex min-w-0 items-center gap-2">
              {selected ? (
                <>
                  <RelativeTime iso={selected.started_at} className="text-sm" />
                  <RunStatusIndicator status={rowStatusFromRunStatus(selected.status)} />
                </>
              ) : (
                "Selected run"
              )}
            </span>
            <ChevronDownIcon aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        {/* Capped and scrollable: the picker lists every run loaded into the Runs tab,
            which grows with each "Load more" and would otherwise run off the screen. */}
        <DropdownMenuContent className="max-h-80 w-72 overflow-y-auto">
          <DropdownMenuRadioGroup value={selectedRunId} onValueChange={onSelectRun}>
            {runs.map((run) => (
              <DropdownMenuRadioItem key={run.id} value={run.id}>
                <span className="flex min-w-0 items-center gap-2">
                  <RelativeTime iso={run.started_at} className="text-sm" />
                  <RunStatusIndicator status={rowStatusFromRunStatus(run.status)} />
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/**
 * One run's artifact — the only place `GET /runs/{id}` is called.
 *
 * `useRun` polls this on its own while the run is active (`runIsActive`), so a run watched
 * from `pending` through `processing` to `completed` fills in its viewer without the user
 * doing anything, and the polling stops by itself the moment it lands.
 */
function RunOutput({ runId, origin }: { runId: string; origin: string }) {
  const { data: run, isPending, isError, error } = useRun(runId);

  if (isPending) return <Skeleton className="h-64 w-full rounded-lg" />;

  if (isError) {
    return (
      <p className="rounded-lg border border-border bg-card p-6 text-sm text-status-failed">
        {error.message}
      </p>
    );
  }

  // State 2 of 4: the run is still going. A skeleton plus a live status, rather than an
  // empty viewer — there is genuinely no artifact yet, and saying so is more useful than
  // rendering zero lines as though that were the result.
  if (isActiveRunStatus(run.status)) {
    return (
      <div className="space-y-3 rounded-lg border border-border bg-card p-6">
        <div className="flex items-center gap-2 text-sm text-foreground">
          <RunStatusIndicator status={rowStatusFromRunStatus(run.status)} />
          <span className="text-muted-foreground">
            Generating… this page updates on its own when the crawl finishes.
          </span>
        </div>
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }

  // State 3 of 4: the run failed. Show why — an empty viewer for a failed run is the
  // specific outcome this ticket's acceptance criteria rule out.
  if (run.status === "failed") {
    return (
      <div className="space-y-3 rounded-lg border border-border bg-card p-6">
        <div className="flex items-center gap-2">
          <RunStatusIndicator status="failed" />
          <span className="text-sm text-muted-foreground">
            This run produced no llms.txt.
          </span>
        </div>
        <pre className="max-h-64 overflow-auto rounded-md bg-status-failed-surface px-4 py-3 font-mono text-xs whitespace-pre-wrap break-words text-status-failed">
          {run.error ?? "The run failed, but recorded no error message."}
        </pre>
      </div>
    );
  }

  // A completed run with no artifact. Not one of the ticket's four states because it should
  // not happen — a run that completes writes its `llms_txt` — but "completed" and "has an
  // artifact" are separate facts in the response, and rendering an empty viewer for this
  // would look identical to a crawl that legitimately found nothing.
  if (run.llms_txt === null || run.llms_txt === undefined) {
    return (
      <p className="rounded-lg border border-border bg-card p-6 text-sm text-muted-foreground">
        This run completed without storing an llms.txt.
      </p>
    );
  }

  // State 4 of 4: success.
  return <LlmsTxtViewer content={run.llms_txt} origin={origin} />;
}
