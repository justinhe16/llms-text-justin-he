"use client";

import { Button } from "@/components/ui/button";
import type { StatsWindow } from "@/lib/api/runs";
import { STATS_WINDOWS } from "@/lib/crawls/use-detail-view";

/** The full words behind "1d" / "7d" / "14d", for the accessible name. "7d" read aloud is
 * "seven dee", which is not what it means. */
const WINDOW_LABELS: Record<StatsWindow, string> = {
  "1d": "Last 24 hours",
  "7d": "Last 7 days",
  "14d": "Last 14 days",
};

interface TrendsWindowSwitcherProps {
  value: StatsWindow;
  onChange: (next: StatsWindow) => void;
  /** Disables every button while the panel has no data to switch between yet. */
  disabled?: boolean;
}

/**
 * The 1d / 7d / 14d selector above the Trends panel.
 *
 * ## Buttons with `aria-pressed`, not a `<Tabs>` and not a `<select>`
 *
 * Three short, always-visible options that change one view is exactly a toggle group. A
 * second `<Tabs>` nested inside the page's tabs would put two tablists on one screen and make
 * "which tab am I on" ambiguous to a screen reader; a `<select>` hides two of three choices
 * behind a click for no gain at this size.
 *
 * `aria-pressed` rather than `aria-current` because these are toggle buttons, not links to
 * places. The `<div role="group">` with an `aria-label` is what makes the three announce as
 * one control instead of three unrelated buttons that happen to sit together.
 *
 * ## No local state
 *
 * The selected window lives in the URL (`lib/crawls/use-detail-view.ts`), so this component
 * is fully controlled and holds nothing of its own. That is what makes
 * `?tab=trends&window=14d` restore this control's position on load without any effect
 * synchronising a `useState` back to the query string.
 */
export function TrendsWindowSwitcher({
  value,
  onChange,
  disabled = false,
}: TrendsWindowSwitcherProps) {
  return (
    <div
      role="group"
      aria-label="Time window"
      className="inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1"
    >
      {STATS_WINDOWS.map((option) => {
        const isSelected = option === value;
        return (
          <Button
            key={option}
            type="button"
            size="sm"
            // `secondary` for the selected one and `ghost` for the rest. Both keep the
            // Button base class's `focus-visible:ring-3`, which is a box-shadow ring rather
            // than an outline — worth knowing, because Tailwind v4's `outline-none` (also on
            // that base class) sets `--tw-outline-style: none`, so a `focus-visible:outline-2`
            // written here instead would render a 2px outline with no style and paint
            // nothing at all, while passing every static check.
            variant={isSelected ? "secondary" : "ghost"}
            aria-pressed={isSelected}
            aria-label={WINDOW_LABELS[option]}
            disabled={disabled}
            onClick={() => onChange(option)}
            className={isSelected ? "font-medium" : "text-muted-foreground"}
          >
            {option}
          </Button>
        );
      })}
    </div>
  );
}
