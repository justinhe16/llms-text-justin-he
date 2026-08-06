"use client";

import { Play, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ownerIdentity } from "@/lib/crawls/owner";
import { useTriggerRun } from "@/lib/query/use-trigger-run";

type RunNowButtonProps = {
  websiteId: string;
  /** The website's owner. Compared against the signed-in user by the parent, which already
   * has both — this component is told the answer rather than resolving identity itself. */
  ownerUserId: string;
  isOwner: boolean;
  /** Whether this website already has a `pending`/`processing` run. Derived by the parent
   * from the run history it is already polling (`anyRunActive`), so this component adds no
   * second request and no second definition of "still running". */
  hasActiveRun: boolean;
  /** Select a run in the Output tab — used for the `409`, where the useful reaction is to
   * show the run that is already in flight rather than the error. */
  onShowRun: (runId: string) => void;
};

/**
 * "Run now" — `POST /websites/{id}/runs`.
 *
 * ## Visible to everyone, enabled for one person
 *
 * A non-owner sees the button disabled with a tooltip naming who can use it, rather than
 * not seeing it at all. This app is deliberately not multi-tenant (ARCHITECTURE.md §4):
 * every signed-in user can read every website, so "why can't I run this" is a question a
 * non-owner will genuinely have, and a missing control answers it worse than a disabled one.
 *
 * The gate is UI only, and is not what actually protects the endpoint — `require_owner` in
 * the service does that, and returns `403` regardless of what this component renders.
 *
 * ## Three reasons the button is disabled, and they are not interchangeable
 *
 * Not the owner, a run is already going, and a request is in flight all disable it, but each
 * says something different and the tooltip says which. A single "disabled" with no
 * explanation is the version of this control that generates support questions.
 *
 * A disabled `<button>` fires no pointer events, so Radix's tooltip would never open on
 * one — hence the `<span>` wrapper that carries the trigger. That is the standard workaround
 * and the reason the markup is shaped this way rather than putting the trigger on the button
 * itself.
 */
export function RunNowButton({
  websiteId,
  ownerUserId,
  isOwner,
  hasActiveRun,
  onShowRun,
}: RunNowButtonProps) {
  const trigger = useTriggerRun({
    onFailure: (failure) => {
      // The `409` case. `useTriggerRun` has already toasted the backend's message ("This
      // website already has a run in progress"); this navigates to the run it named, which
      // is the part a message cannot do. Every other failure — `403`, `429`, `503`, `404` —
      // is fully served by that toast and needs nothing here.
      if (failure.existingRunId !== null) onShowRun(failure.existingRunId);
    },
  });

  const isSubmitting = trigger.isPending;
  const disabled = !isOwner || hasActiveRun || isSubmitting;

  // Ordered by precedence, matching the `disabled` expression above: ownership is checked
  // first because it is the only reason that will not resolve on its own.
  const disabledReason = !isOwner
    ? `Only the owner (${ownerIdentity(ownerUserId, null).label}) can start a run.`
    : hasActiveRun
      ? "A run is already in progress. It will finish on its own."
      : isSubmitting
        ? "Starting a run…"
        : null;

  const label = hasActiveRun || isSubmitting ? "Running…" : "Run now";

  const button = (
    <Button
      type="button"
      disabled={disabled}
      onClick={() => trigger.mutate(websiteId)}
      // `aria-disabled` alongside the real `disabled`: the attribute is what a screen
      // reader announces together with the tooltip below, which is the only place the
      // reason is written down.
      aria-disabled={disabled}
    >
      {hasActiveRun || isSubmitting ? (
        <RefreshCw className="animate-spin" aria-hidden="true" />
      ) : (
        <Play aria-hidden="true" />
      )}
      {label}
    </Button>
  );

  if (disabledReason === null) return button;

  return (
    <Tooltip>
      {/* `asChild` onto a focusable span, not the disabled button: a disabled button
          dispatches no pointer events, so a tooltip anchored to it never opens — which
          would silently drop the explanation for the one state that most needs one. */}
      <TooltipTrigger asChild>
        <span tabIndex={0} className="inline-flex rounded-md">
          {button}
        </span>
      </TooltipTrigger>
      <TooltipContent>{disabledReason}</TooltipContent>
    </Tooltip>
  );
}
