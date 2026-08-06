"use client";

import { useId } from "react";
import { Loader2 } from "lucide-react";

import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ScheduleIntervalMinutes } from "@/lib/api/schedules";
import { formatIntervalMinutes } from "@/lib/crawls/row-status";
import { SCHEDULE_INTERVAL_OPTIONS } from "@/lib/crawls/schedule-presets";
import type { ScheduleDraft } from "@/lib/crawls/use-schedule-editor";
import { cn } from "@/lib/utils";

/**
 * The two things a schedule actually has: whether it runs, and how often.
 *
 * ## Disabled, never hidden
 *
 * A non-owner sees both controls in their real positions, greyed out, with the reason
 * underneath. This app is deliberately not multi-tenant (ARCHITECTURE.md §4) — every
 * signed-in user can read every website — so "what is this site's schedule" is a legitimate
 * question for someone who cannot change it, and an empty panel answers it worse than a
 * disabled one. The same argument `RunNowButton` makes for the Run now button.
 *
 * As there, the gate is presentation only. `require_owner` in the service is what actually
 * refuses the write, with a `403`, no matter what this component renders.
 *
 * ## The presets are not listed here
 *
 * `SCHEDULE_INTERVAL_OPTIONS` (lib/crawls/schedule-presets.ts) is derived from the generated
 * OpenAPI types, so a fifth preset added backend-side appears in this radio group after
 * `npm run gen:api` with no edit to this file — and, until someone writes its label, fails
 * the build rather than silently going missing.
 */
export function ScheduleControls({
  value,
  storedIntervalMinutes,
  isSaving,
  needsIntervalChoice,
  canEdit,
  disabledReason,
  onActiveChange,
  onIntervalChange,
}: {
  value: ScheduleDraft;
  /** The interval as stored, straight off the response. Used only to name a value that is
   * not one of the presets; `null` when there is no schedule row yet. */
  storedIntervalMinutes: number | null;
  isSaving: boolean;
  needsIntervalChoice: boolean;
  canEdit: boolean;
  /** Why the controls are inert, for the tooltip. `null` when they are not. */
  disabledReason: string | null;
  onActiveChange: (active: boolean) => void;
  onIntervalChange: (minutes: ScheduleIntervalMinutes) => void;
}) {
  const intervalLabelId = useId();
  const intervalFieldId = useId();

  // The toggle has one extra reason to be inert that the radio group does not: with a stored
  // interval outside the four presets there is no valid `interval_minutes` to send alongside
  // `active`, because `UpsertScheduleRequest` requires both on every write. Picking a preset
  // clears it immediately.
  const toggleDisabled = !canEdit || needsIntervalChoice;

  const toggle = (
    <span className="inline-flex items-center gap-2.5">
      <Switch
        checked={value.active}
        onCheckedChange={onActiveChange}
        disabled={toggleDisabled}
        aria-disabled={toggleDisabled}
        // A fixed accessible name, rather than letting the changing "Active"/"Paused" word
        // become it: `role="switch"` already announces on/off, so a name that also flipped
        // would read as "Paused, switch, off" and say the same thing twice.
        aria-label="Run this crawl on a schedule"
      />
      <span
        className={cn(
          "text-sm font-medium",
          value.active ? "text-foreground" : "text-muted-foreground"
        )}
      >
        {value.active ? "Active" : "Paused"}
      </span>
    </span>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-foreground">Schedule</h2>

        <span className="inline-flex items-center gap-3">
          {/* Inline and subtle, never a panel-wide blocking state: the debounce means this
              indicator is visible for most of a second on every change, and a panel that
              greyed itself out that often would be unusable. */}
          <span
            aria-live="polite"
            className={cn(
              "inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-opacity",
              isSaving ? "opacity-100" : "opacity-0"
            )}
          >
            <Loader2 aria-hidden="true" className="size-3 animate-spin" />
            {isSaving ? "Saving…" : ""}
          </span>

          {disabledReason === null ? (
            toggle
          ) : (
            <Tooltip>
              {/* A disabled control dispatches no pointer events, so a tooltip anchored to it
                  never opens — the focusable wrapper is the standard workaround, and the same
                  one `RunNowButton` uses. */}
              <TooltipTrigger asChild>
                <span tabIndex={0} className="inline-flex rounded-md">
                  {toggle}
                </span>
              </TooltipTrigger>
              <TooltipContent>{disabledReason}</TooltipContent>
            </Tooltip>
          )}
        </span>
      </div>

      <div className="space-y-3">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span id={intervalLabelId} className="text-sm text-muted-foreground">
            Interval
          </span>
          {needsIntervalChoice && storedIntervalMinutes !== null && (
            <span className="text-xs text-muted-foreground/80">
              currently {formatIntervalMinutes(storedIntervalMinutes)} — not one of the presets
              below
            </span>
          )}
        </div>

        <RadioGroup
          id={intervalFieldId}
          aria-labelledby={intervalLabelId}
          // `""` rather than `undefined` keeps this a controlled group with nothing selected,
          // which is the honest rendering of a stored interval that is not one of the four.
          value={value.intervalMinutes === null ? "" : String(value.intervalMinutes)}
          onValueChange={(next) => onIntervalChange(Number(next) as ScheduleIntervalMinutes)}
          disabled={!canEdit}
          className="grid-cols-1 gap-3 sm:grid-cols-2"
        >
          {SCHEDULE_INTERVAL_OPTIONS.map((option) => {
            const itemId = `${intervalFieldId}-${option.minutes}`;
            return (
              <div key={option.minutes} className="flex items-center gap-2.5">
                <RadioGroupItem id={itemId} value={String(option.minutes)} />
                <Label
                  htmlFor={itemId}
                  className={cn(
                    "font-normal",
                    canEdit ? "cursor-pointer text-foreground" : "text-muted-foreground"
                  )}
                >
                  {option.label}
                </Label>
              </div>
            );
          })}
        </RadioGroup>
      </div>
    </div>
  );
}
