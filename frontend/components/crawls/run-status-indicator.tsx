import {
  rowStatusLabel,
  rowStatusToken,
  type RowStatus,
  type StatusToken,
} from "@/lib/crawls/row-status";
import { cn } from "@/lib/utils";

// Full class names, never `` `bg-status-${token}` ``. Tailwind builds its stylesheet by
// scanning source files for literal class strings; a name assembled at runtime is a name
// Tailwind never saw, so the utility is simply absent from the build and the dot renders
// transparent. That failure is invisible to tsc, to eslint and to `next build`, which is
// exactly the class of bug the rendered-output smoke test exists for — but the cheaper fix
// is to not write the interpolation in the first place.
//
// The token names themselves come from app/globals.css, which is the only file that decides
// what colour a status is. Nothing here reaches for `emerald-*` or `rose-*` directly.
const DOT_CLASS: Record<StatusToken, string> = {
  completed: "bg-status-completed",
  processing: "bg-status-processing",
  failed: "bg-status-failed",
  idle: "bg-status-idle",
};

const LABEL_CLASS: Record<StatusToken, string> = {
  completed: "text-status-completed",
  processing: "text-status-processing",
  failed: "text-status-failed",
  idle: "text-status-idle",
};

/** Row background while a row is briefly highlighted after a status change
 * (lib/crawls/use-status-change-highlight.ts). The `-surface` tints are the pale halves of
 * the same four tokens. */
export const HIGHLIGHT_SURFACE_CLASS: Record<StatusToken, string> = {
  completed: "bg-status-completed-surface",
  processing: "bg-status-processing-surface",
  failed: "bg-status-failed-surface",
  idle: "bg-status-idle-surface",
};

interface RunStatusIndicatorProps {
  status: RowStatus;
  className?: string;
}

/**
 * A coloured dot and a label — the Status column, and the same thing again in the mobile
 * card.
 *
 * `running` and `queued` share the amber token deliberately (see row-status.ts): they are
 * two phases of one in-progress state, told apart by their word and by the dot pulsing on
 * only one of them, rather than by adding a fifth colour to a four-colour legend. The pulse
 * is `animate-pulse` — an opacity fade, not motion — because a table that polls every three
 * seconds should not also be jittering.
 *
 * The dot is `aria-hidden`: it carries no information the adjacent label does not, and an
 * unlabelled decorative element announced between cells is just noise.
 */
export function RunStatusIndicator({ status, className }: RunStatusIndicatorProps) {
  const token = rowStatusToken(status);
  const isMoving = status === "running";

  return (
    <span className={cn("inline-flex items-center gap-2 whitespace-nowrap", className)}>
      <span
        aria-hidden="true"
        className={cn("size-2 shrink-0 rounded-full", DOT_CLASS[token], isMoving && "animate-pulse")}
      />
      <span className={cn("text-sm", LABEL_CLASS[token])}>{rowStatusLabel(status)}</span>
    </span>
  );
}
