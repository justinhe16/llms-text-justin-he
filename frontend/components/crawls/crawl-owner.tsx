"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ownerIdentity } from "@/lib/crawls/owner";
import { cn } from "@/lib/utils";

interface CrawlOwnerProps {
  /** `WebsiteListItem.user_id` — who added this website. */
  userId: string;
  /** The signed-in user's id, or `null` while the session is still resolving. */
  currentUserId: string | null;
  className?: string;
}

/**
 * The Owner column. "you" for your own rows; a short, honest form of the owner's id for
 * everybody else's.
 *
 * This is where the gap documented in lib/crawls/owner.ts shows up on screen: the list
 * endpoint returns `user_id` and no handle or avatar, so a real `@handle` is renderable for
 * exactly one person. Rather than invent one, the column shows a monogram chip and the first
 * eight characters of the id, with the full value in a tooltip — enough to tell two owners
 * apart and to see at a glance which rows are yours, which is what this column is actually
 * for. It becomes the handle the ticket describes the moment the backend returns one.
 *
 * Why the column exists at all before then: ARCHITECTURE.md §4 makes every website readable
 * by every signed-in user, so "whose crawl am I looking at" is a question this table has to
 * answer, and an unlabelled shared list is worse than a partially labelled one.
 */
export function CrawlOwner({ userId, currentUserId, className }: CrawlOwnerProps) {
  const owner = ownerIdentity(userId, currentUserId);

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn("inline-flex min-w-0 cursor-default items-center gap-2", className)}>
          <span
            aria-hidden="true"
            className={cn(
              "flex size-5 shrink-0 items-center justify-center rounded-full text-[10px] leading-none font-medium",
              owner.isYou
                ? "bg-primary/10 text-primary"
                : "bg-muted text-muted-foreground"
            )}
          >
            {owner.monogram}
          </span>
          <span
            className={cn(
              "truncate",
              owner.isYou
                ? "text-sm text-foreground"
                : "font-mono text-xs text-muted-foreground"
            )}
          >
            {owner.label}
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <span className="font-mono">{owner.userId}</span>
      </TooltipContent>
    </Tooltip>
  );
}
