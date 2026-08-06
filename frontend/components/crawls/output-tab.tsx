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
import { formatRelativeTime } from "@/lib/format/time";

import { LlmsTxtViewer } from "./llms-txt-viewer";
import { RunStatusBadge, RunStatusDot, runStatusLabel } from "./run-status-indicator";

type OutputTabProps = {
  origin: string;
  runs: RunListItem[];
  isLoadingRuns: boolean;
  /** From `?run=` — `null` when the user has not picked one, in which case this component
   * falls back to the most recent completed run. */
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
  selectedRunId,
  onSelectRun,
  canRun,
}: OutputTabProps) {
  // `runs` arrives newest-first from the API, so the first match in each pass is the most
  // recent one — no sorting here, and no second opinion about the ordering the backend's
  // keyset pagination already guarantees.
  const selectedRun =
    runs.find((run) => run.id === selectedRunId) ??
    runs.find((run) => run.status === "completed") ??
    runs[0];

  if (isLoadingRuns) return <Skeleton className="h-64 w-full rounded-lg" />;

  // State 1 of 4: no runs at all.
  if (runs.length === 0) {
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

  // `?run=` names a run that is not on any loaded page — a deep link, or a run reached from
  // the `409`. It is still perfectly displayable: `GET /runs/{id}` needs only the id, and
  // whether the run happens to be in this component's list is irrelevant to it.
  const runIdToShow = selectedRun?.id ?? selectedRunId;
  if (runIdToShow === null || runIdToShow === undefined) return null;

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
            <span className="flex items-center gap-2">
              {selected ? (
                <>
                  <RunStatusDot status={selected.status} />
                  {formatRelativeTime(selected.started_at)}
                  <span className="text-muted-foreground">
                    · {runStatusLabel(selected.status)}
                  </span>
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
                <span className="flex items-center gap-2">
                  <RunStatusDot status={run.status} />
                  {formatRelativeTime(run.started_at)}
                  <span className="text-muted-foreground">· {runStatusLabel(run.status)}</span>
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
          <RunStatusBadge status={run.status} />
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
          <RunStatusBadge status="failed" />
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
