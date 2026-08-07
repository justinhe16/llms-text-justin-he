"use client";

import Image from "next/image";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useUser } from "@/lib/auth/use-user";
import { ownerPill } from "@/lib/crawls/owner";
import { cn } from "@/lib/utils";

interface CrawlOwnerProps {
  /** `WebsiteListItem.user_id` / `WebsiteResponse.user_id` — who added this website. */
  userId: string;
  /** Make the pill a tab stop so its tooltip is keyboard-reachable. `false` in the table:
   * one extra tab stop per row is the cost relative-time.tsx already refuses to pay for
   * the Last-run cell. `true` on the detail header, where the pill is one element on the
   * page and the tab stop is free. */
  focusable?: boolean;
  className?: string;
}

/**
 * The Owner pill. Your GitHub avatar and "you" on your own row; a short, honest form of the
 * owner's id — 8 hex characters, monospace — for everybody else's, with the full `user_id`
 * in a tooltip either way (an `@handle` instead, for your own row, when the session has one).
 *
 * This calls `useUser()` itself rather than taking the signed-in user as a prop. `@supabase/
 * ssr`'s `createBrowserClient` caches a singleton in the browser
 * (node_modules/@supabase/ssr/dist/main/createBrowserClient.js:9-16), so every row's call
 * shares one client and one auth state machine, and `use-user.ts`'s `sameUser` guard returns
 * the previous object identity on `TOKEN_REFRESHED`, so rows do not re-render on the refresh
 * timer. That trade-off holds for the row counts this table renders; a paginated table of
 * hundreds of rows would justify hoisting `useUser()` back up and passing the viewer down.
 *
 * This is where the gap documented in lib/crawls/owner.ts shows up on screen: the list
 * endpoint returns `user_id` and no handle or avatar for anybody else, so a real `@handle` is
 * renderable for exactly one person — you. Everybody else's pill is the id, honestly
 * shortened, with the full value a tooltip away. It becomes the handle for everyone the
 * moment the backend returns one.
 *
 * Why the column exists at all before then: ARCHITECTURE.md §4 makes every website readable
 * by every signed-in user, so "whose crawl am I looking at" is a question this table has to
 * answer, and an unlabelled shared list is worse than a partially labelled one.
 */
export function CrawlOwner({ userId, focusable = false, className }: CrawlOwnerProps) {
  const { user } = useUser();
  const owner = ownerPill(userId, user);
  const tooltip = owner.handle ?? owner.userId;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={focusable ? 0 : undefined}
          className={cn(
            "inline-flex max-w-full cursor-default rounded-4xl outline-none",
            focusable && "focus-visible:ring-3 focus-visible:ring-ring/50",
            className
          )}
        >
          <Badge
            variant="secondary"
            className={cn(
              "max-w-full gap-1.5 font-normal",
              owner.isYou
                ? "bg-primary/10 text-primary"
                : "bg-muted font-mono text-muted-foreground"
            )}
          >
            {owner.avatarUrl !== null ? (
              <Image
                data-icon="inline-start"
                src={owner.avatarUrl}
                alt=""
                width={16}
                height={16}
                className="size-4 shrink-0 rounded-full"
              />
            ) : owner.initial !== null ? (
              <span
                data-icon="inline-start"
                aria-hidden="true"
                className="flex size-4 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[9px] leading-none font-medium"
              >
                {owner.initial}
              </span>
            ) : null}
            {owner.text}
          </Badge>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <span className={owner.handle === null ? "font-mono" : undefined}>{tooltip}</span>
      </TooltipContent>
    </Tooltip>
  );
}
