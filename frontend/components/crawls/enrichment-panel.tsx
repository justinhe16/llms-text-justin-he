"use client";

import { useId } from "react";
import { Lock } from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ENRICHMENT_HELP, ENRICHMENT_LABEL } from "@/lib/crawls/enrichment-copy";
import { ownerIdentity } from "@/lib/crawls/owner";
import { useUpdateWebsite } from "@/lib/query/use-update-website";
import { useWebsite } from "@/lib/query/use-website";

import { CrawlsError } from "./crawls-error";

/** Matches the loaded card's shape, mirroring `ScheduleTab`'s own skeleton. */
function EnrichmentPanelSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="mx-auto w-full max-w-xl space-y-3 rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-start gap-3">
        <Skeleton className="mt-0.5 size-4 shrink-0 rounded-[4px]" />
        <div className="w-full space-y-2">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-3 w-full max-w-sm" />
          <Skeleton className="h-3 w-40" />
        </div>
      </div>
    </div>
  );
}

/**
 * The per-website LLM enrichment opt-in — `PATCH /websites/{id}`'s one field, in a card
 * rendered beside `ScheduleTab` inside the existing "Schedule" tab (see `website-detail.tsx`).
 *
 * **No fifth tab.** The Schedule tab is already the only place an owner changes anything
 * about a website, already renders the exact "Only the owner (…) can …" copy this panel
 * reuses, and `WebsiteDetail` already computes and passes down `isOwner`/`ownerUserId`. A
 * fifth tab would mean editing `DETAIL_TABS`, the URL vocabulary, and `useDetailView`'s
 * guard — real churn for one checkbox. (A rename of the tab label to "Settings" was
 * considered and rejected for the same reason `schedule-tab.tsx`'s own docstring would give:
 * it would change a shipped `?tab=` value.)
 *
 * **No debounce, no draft module — deliberately unlike `use-schedule-editor.ts`.** That
 * module exists entirely for a ~500ms debounce over TWO controls (active/interval) sharing
 * one `PUT` request body; neither condition holds for a single boolean. This panel sends on
 * every change and renders `mutation.isPending ? mutation.variables.enrichWithLlm :
 * website.enrich_with_llm` for optimism while the request is in flight; on error,
 * `useUpdateWebsite`'s invalidation is what makes the server's real value show back through
 * once the refetch lands.
 */
export function EnrichmentPanel({
  websiteId,
  ownerUserId,
  isOwner,
}: {
  websiteId: string;
  /** The website's owner. `null` while the website query is still resolving. */
  ownerUserId: string | null;
  isOwner: boolean;
}) {
  const checkboxId = useId();
  const websiteQuery = useWebsite(websiteId);
  const website = websiteQuery.data;
  const mutation = useUpdateWebsite();

  if (websiteQuery.isPending) return <EnrichmentPanelSkeleton />;

  if (websiteQuery.isError) {
    return (
      <CrawlsError
        error={websiteQuery.error}
        onRetry={() => void websiteQuery.refetch()}
        isRetrying={websiteQuery.isFetching}
        hasStaleData={false}
      />
    );
  }

  // Unreachable: `isPending`/`isError` are exhaustive for `useQuery`'s three states, so
  // `website` is always defined here — this is a type guard, not a real branch, matching the
  // same "belt and braces" narrowing `WebsiteDetail` (website-detail.tsx) uses for the same
  // `useWebsite` hook.
  if (website === undefined) return null;

  const checked = mutation.isPending
    ? (mutation.variables?.enrichWithLlm ?? website.enrich_with_llm)
    : website.enrich_with_llm;

  // Same precedence rule `ScheduleTab` states for its own `disabledReason`: not being the
  // owner is the only reason this control is ever inert, so there is exactly one branch here
  // rather than a precedence list.
  const disabledReason = isOwner
    ? null
    : ownerUserId === null
      ? "Only the owner can change this."
      : `Only the owner (${ownerIdentity(ownerUserId, null).label}) can change this.`;

  const checkbox = (
    <Checkbox
      id={checkboxId}
      checked={checked}
      disabled={!isOwner}
      aria-disabled={!isOwner}
      onCheckedChange={(value) => mutation.mutate({ websiteId, enrichWithLlm: value === true })}
    />
  );

  return (
    <div className="mx-auto w-full max-w-xl space-y-3 rounded-lg border border-border bg-card p-6">
      <div className="flex items-start gap-3">
        {disabledReason === null ? (
          checkbox
        ) : (
          <Tooltip>
            {/* A disabled control dispatches no pointer events, so a tooltip anchored to it
                never opens — the same focusable-wrapper workaround `ScheduleControls` uses
                for its own inert toggle. */}
            <TooltipTrigger asChild>
              <span tabIndex={0} className="inline-flex rounded-sm">
                {checkbox}
              </span>
            </TooltipTrigger>
            <TooltipContent>{disabledReason}</TooltipContent>
          </Tooltip>
        )}

        <div className="space-y-1">
          <label
            htmlFor={checkboxId}
            className={
              isOwner
                ? "cursor-pointer text-sm font-medium text-foreground"
                : "text-sm font-medium text-muted-foreground"
            }
          >
            {ENRICHMENT_LABEL}
          </label>
          <p className="text-xs text-muted-foreground">{ENRICHMENT_HELP}</p>
          <p className="text-xs text-muted-foreground">
            Applies to the next run; past runs are unchanged.
          </p>
        </div>
      </div>

      {disabledReason !== null && (
        <p className="flex items-start gap-2 border-t border-border pt-3 text-sm text-muted-foreground">
          <Lock aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span>{disabledReason}</span>
        </p>
      )}
    </div>
  );
}
