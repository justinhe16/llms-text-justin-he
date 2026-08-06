"use client";

import { Lock } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import type { RunListItem } from "@/lib/api/runs";
import { ownerIdentity } from "@/lib/crawls/owner";
import { useScheduleEditor } from "@/lib/crawls/use-schedule-editor";
import { useSchedule } from "@/lib/query/use-schedule";

import { CrawlsError } from "./crawls-error";
import { ScheduleControls } from "./schedule-controls";
import { ScheduleTimeline } from "./schedule-timeline";

/** Matches the loaded panel's shape — a header row, a two-column block of four options, and
 * two timeline lines — so opening the tab does not reflow once the response lands. */
function ScheduleSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="mx-auto w-full max-w-xl space-y-6 rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-center justify-between">
        <Skeleton className="h-5 w-20" />
        <Skeleton className="h-5 w-24" />
      </div>
      <div className="space-y-3">
        <Skeleton className="h-4 w-16" />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-28" />
        </div>
      </div>
      <div className="space-y-3 border-t border-border pt-6">
        <Skeleton className="h-4 w-56" />
        <Skeleton className="h-4 w-48" />
      </div>
    </div>
  );
}

/**
 * The Schedule tab — `GET`/`PUT /websites/{id}/schedule`.
 *
 * ## The query lives here, not in `WebsiteDetail`
 *
 * `WebsiteDetail` hoists the run history because three of its children need the same list.
 * Nothing outside this panel reads the schedule, so hoisting it would move a request away
 * from its only consumer for no benefit — the same reasoning that keeps `GET /runs/{id}`
 * inside the Output tab. Radix mounts only the active tab's content, so this request is not
 * made at all until someone opens the tab.
 *
 * The one thing that *is* passed down is `latestRun`. "Last run … completed" needs a status,
 * and `ScheduleResponse` has only `last_run_at` with no status beside it — so the status has
 * to come from the run history the page has already fetched. Issuing a second runs query here
 * would not just be wasteful, it could disagree with the status in the page header.
 *
 * ## `null` is a state, not an error
 *
 * `GET` answers `200` with a `null` body for a website that has never had a schedule. That is
 * "off, daily preselected" (see `scheduleToDraft`), which is what lets a first-time enable be
 * a single click — the alternative, an empty panel with a "Create schedule" button, makes the
 * user perform a setup step that carries no information.
 */
export function ScheduleTab({
  websiteId,
  ownerUserId,
  isOwner,
  latestRun,
}: {
  websiteId: string;
  /** The website's owner. `null` while the website query is still resolving. */
  ownerUserId: string | null;
  isOwner: boolean;
  latestRun: RunListItem | null;
}) {
  const scheduleQuery = useSchedule(websiteId);
  const schedule = scheduleQuery.data;

  const editor = useScheduleEditor({ websiteId, schedule });

  if (scheduleQuery.isPending) return <ScheduleSkeleton />;

  if (scheduleQuery.isError) {
    return (
      <CrawlsError
        error={scheduleQuery.error}
        onRetry={() => void scheduleQuery.refetch()}
        isRetrying={scheduleQuery.isFetching}
        hasStaleData={false}
      />
    );
  }

  const canEdit = isOwner;

  // Ordered by precedence, and the two reasons are not interchangeable: not being the owner
  // is permanent for this viewer, while an unsupported stored interval is one click away from
  // resolving. A single unexplained "disabled" is the version of this panel that generates
  // support questions.
  const disabledReason = !canEdit
    ? ownerUserId === null
      ? "Only the owner can change this schedule."
      : `Only the owner (${ownerIdentity(ownerUserId, null).label}) can change this schedule.`
    : editor.needsIntervalChoice
      ? "This schedule uses an interval that is not one of the presets. Choose one below to change it."
      : null;

  return (
    <div className="mx-auto w-full max-w-xl space-y-6 rounded-lg border border-border bg-card p-6">
      <ScheduleControls
        value={editor.value}
        storedIntervalMinutes={schedule?.interval_minutes ?? null}
        isSaving={editor.isSaving}
        needsIntervalChoice={editor.needsIntervalChoice}
        canEdit={canEdit}
        disabledReason={disabledReason}
        onActiveChange={editor.setActive}
        onIntervalChange={editor.setIntervalMinutes}
      />

      <div className="border-t border-border pt-6">
        <ScheduleTimeline
          active={editor.value.active}
          // Straight from the server, never derived from the draft. While a change is in
          // flight the controls show what was asked for, but the next run time is the
          // server's to compute (interval, jitter, and when it last ran) — inventing one here
          // would put a number on screen that nothing will ever confirm.
          nextRunAt={schedule?.next_run_at ?? null}
          intervalMinutes={editor.value.intervalMinutes}
          latestRun={latestRun}
        />
      </div>

      {!canEdit && (
        <p className="flex items-start gap-2 border-t border-border pt-6 text-sm text-muted-foreground">
          <Lock aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          {/* No `@handle`: the API exposes `user_id` and nothing else, and there is no
              `/users` endpoint to resolve it (see lib/crawls/owner.ts). A short id is honest
              where a fabricated handle is the kind of thing someone pastes into a search
              box. */}
          <span>{disabledReason}</span>
        </p>
      )}
    </div>
  );
}
