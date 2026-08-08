import { SparklesIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { RunListItem } from "@/lib/api/runs";
import { ENRICHMENT_FALLBACK_BADGE, ENRICHMENT_FALLBACK_EXPLANATION } from "@/lib/crawls/enrichment-copy";
import { runEnrichmentFellBack } from "@/lib/crawls/run-display";

/**
 * The badge that appears beside `RunStatusIndicator` when a run asked for model-assisted
 * summarization and did not get it (PER-194) — `lib/crawls/run-display.ts`'s
 * `runEnrichmentFellBack` is the predicate deciding when.
 *
 * Mirrors `RunStatusIndicator` (run-status-indicator.tsx) in shape: a small presentational
 * component taking one run and rendering `null` when its predicate is false, so a call site
 * never has to guard the render itself. Built from `components/ui/badge.tsx`
 * (`variant="outline"`, the same neutral treatment `run.trigger`'s pill uses in
 * `runs-tab.tsx`) inside a `Tooltip` carrying the full sentence — the same disabled-control
 * tooltip idiom `ScheduleControls` (schedule-controls.tsx) uses for its own "why is this
 * inert" explanation, applied here to "why does this row look different."
 */
export function RunEnrichmentBadge({
  run,
}: {
  run: Pick<RunListItem, "status" | "stats">;
}) {
  if (!runEnrichmentFellBack(run)) return null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="gap-1 text-muted-foreground">
          <SparklesIcon aria-hidden="true" className="size-3" />
          {ENRICHMENT_FALLBACK_BADGE}
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{ENRICHMENT_FALLBACK_EXPLANATION}</TooltipContent>
    </Tooltip>
  );
}
