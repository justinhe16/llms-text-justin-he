"use client";

import { UserMenu } from "@/components/auth/user-menu";
import { useUser } from "@/lib/auth/use-user";

/**
 * The avatar and sign-out in the landing page's top-right corner — the only chrome this
 * screen has.
 *
 * The gate is the whole point: `UserMenu` renders a `Skeleton` while the session resolves,
 * which is the right behaviour inside `CrawlsHeader` (a header that is there regardless) and
 * the wrong one here, where a grey pill fading into nothing for every signed-out visitor
 * would be chrome on a page whose requirement is to have none. This renders *nothing at all*
 * unless there is a user.
 *
 * `CrawlsHeader` is deliberately not reused: the landing page has no header, and importing
 * one to hide most of it is how a page ends up with a header nobody asked for.
 */
export function LandingAccount() {
  const { user, isLoading } = useUser();

  if (isLoading || !user) return null;

  return (
    <div className="absolute top-4 right-4 z-20 sm:top-6 sm:right-6">
      <UserMenu />
    </div>
  );
}
