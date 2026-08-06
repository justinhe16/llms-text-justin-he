// The two ways a run's status is shown in this app: a dot (headers, dense rows) and a
// badge (the Runs table's Status column). Both read from ONE map, below, so a status can
// never be amber in one place and stone in another.
//
// PER-161's `/crawls` table needs exactly these — if it landed first, this file should be
// the one that was reused rather than a second copy of the same four colours.

import { cn } from "@/lib/utils";
import { isActiveRunStatus, type RunStatus } from "@/lib/api/run-status";

/**
 * A website with no runs at all. Not a `RunStatus` — Postgres has no such value, and adding
 * one to `RunStatus` to cover a UI state would corrupt the vocabulary
 * `backend/app/features/runs/schemas.py` owns. It is a display concern, so it is spelled
 * here, where display lives.
 */
export type RunStatusOrIdle = RunStatus | "idle";

/**
 * Every status's label and its two colour tokens.
 *
 * The class strings are written out in full rather than composed
 * (`` `bg-status-${status}` ``) because Tailwind resolves utilities by scanning source text
 * for complete class names: an interpolated one is not in the generated stylesheet at all,
 * and the failure mode is an element that renders with no colour rather than an error.
 *
 * The tokens themselves are the ones app/globals.css defines for exactly this purpose —
 * never a raw `emerald-*`/`amber-*`/`rose-*` utility, so recolouring a status stays the
 * one-line edit that file promises.
 */
const STATUS_DISPLAY: Record<
  RunStatusOrIdle,
  { label: string; dot: string; badge: string }
> = {
  pending: {
    label: "Pending",
    dot: "bg-status-idle",
    badge: "bg-status-idle-surface text-status-idle",
  },
  processing: {
    label: "Processing",
    dot: "bg-status-processing",
    badge: "bg-status-processing-surface text-status-processing",
  },
  completed: {
    label: "Completed",
    dot: "bg-status-completed",
    badge: "bg-status-completed-surface text-status-completed",
  },
  failed: {
    label: "Failed",
    dot: "bg-status-failed",
    badge: "bg-status-failed-surface text-status-failed",
  },
  idle: {
    label: "No runs yet",
    dot: "bg-status-idle",
    badge: "bg-status-idle-surface text-status-idle",
  },
};

/** The human-readable name of a status — the badge's text, and the header's status line. */
export function runStatusLabel(status: RunStatusOrIdle): string {
  return STATUS_DISPLAY[status].label;
}

// `idle` is not a real run status, so it can never be "active" — `isActiveRunStatus` is
// only asked about the four values it actually knows.
function statusIsActive(status: RunStatusOrIdle): boolean {
  return status !== "idle" && isActiveRunStatus(status);
}

/**
 * A small filled circle in the status colour. Pulses while the run is still going, which is
 * the one piece of information the colour alone does not carry: `pending` and `idle` are
 * both stone, and only one of them is going to change on its own.
 *
 * `aria-hidden` because it is never the only thing saying what the status is — every call
 * site pairs it with the label as text.
 */
export function RunStatusDot({
  status,
  className,
}: {
  status: RunStatusOrIdle;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        STATUS_DISPLAY[status].dot,
        statusIsActive(status) && "animate-pulse",
        className
      )}
    />
  );
}

/** The status as a tinted pill — the Runs table's Status column. */
export function RunStatusBadge({
  status,
  className,
}: {
  status: RunStatusOrIdle;
  className?: string;
}) {
  const display = STATUS_DISPLAY[status];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-4xl px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        display.badge,
        className
      )}
    >
      <RunStatusDot status={status} />
      {display.label}
    </span>
  );
}
