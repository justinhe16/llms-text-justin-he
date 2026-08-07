"use client";

import { FileDownIcon, LoaderCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useDownloadFullArtifact } from "@/lib/crawls/use-download-full-artifact";

type DownloadFullButtonProps = {
  runId: string;
  origin: string;
  /** Why the control is inert, or `undefined` when it is genuinely usable — passed by
   * `output-tab.tsx`'s `RunOutput` for the three states where the selected run has produced
   * no `llms-full.txt` yet. Absent, not `null`: every call site either has a reason or
   * doesn't, and there is no third "not yet decided" state worth a distinct value for. */
  disabledReason?: string;
};

/**
 * "Download full" — `GET /runs/{id}/llms-full.txt`, via `useDownloadFullArtifact`.
 *
 * ## Disabled, never hidden
 *
 * `output-tab.tsx` renders this control in every branch of `RunOutput`, including the three
 * where the selected run has no artifact to download yet (still running, failed, or completed
 * without one) — with `disabledReason` set so the control renders inert rather than
 * disappearing. A control that vanishes whenever it would be disabled reads as a bug the next
 * time someone goes looking for it; a disabled control with an explanation reads as a state.
 * This is the same "visible to everyone, enabled for one person" shape `RunNowButton`
 * (components/crawls/run-now-button.tsx) already uses, just gated on the artifact's
 * existence instead of on ownership.
 *
 * A download in flight disables the button too, so a second click cannot fire a second
 * request, but it does not go through `disabledReason` or the tooltip below — the
 * "Downloading…" label is its own explanation, and wrapping an already-self-explanatory
 * state in a tooltip would just be noise.
 *
 * A disabled `<button>` fires no pointer events, so Radix's tooltip would never open on one —
 * hence the `<span>` wrapper that carries the trigger, the same workaround `RunNowButton`
 * uses and for the same reason.
 */
export function DownloadFullButton({ runId, origin, disabledReason }: DownloadFullButtonProps) {
  const { download, isDownloading } = useDownloadFullArtifact(runId, origin);

  const disabled = disabledReason !== undefined || isDownloading;

  const button = (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={disabled}
      onClick={download}
      // `aria-disabled` alongside the real `disabled`: the attribute is what a screen reader
      // announces together with the tooltip below, which is the only place the reason is
      // written down.
      aria-disabled={disabled}
    >
      {isDownloading ? (
        <LoaderCircle className="animate-spin" aria-hidden="true" />
      ) : (
        <FileDownIcon aria-hidden="true" />
      )}
      {isDownloading ? "Downloading…" : "Download full"}
    </Button>
  );

  if (disabledReason === undefined) return button;

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
