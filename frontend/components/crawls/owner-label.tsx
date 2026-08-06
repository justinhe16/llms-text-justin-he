"use client";

import { useUser } from "@/lib/auth/use-user";
import { cn } from "@/lib/utils";

/**
 * Who owns a website.
 *
 * ## Why this renders an id and not a name
 *
 * There is no way to get one. `WebsiteResponse.user_id` is a bare UUID
 * (backend/app/features/websites/schemas.py), there is no `/users/{id}` endpoint anywhere in
 * `lib/api/openapi.json`, and `lib/auth/use-user.ts` resolves a handle and avatar only for
 * the *currently signed-in* caller, straight from that caller's own Supabase session — it
 * cannot answer the same question about anybody else. Rendering "you" versus a short id is
 * the honest maximum available today, and it is enough for the job the label actually has:
 * telling a user whether the controls on this page are theirs to use.
 *
 * A real display name is a backend change (a `user` fold on `WebsiteResponse`, or a users
 * endpoint), not something this component can work around, and it is not in this ticket.
 *
 * ## Why the id is shortened
 *
 * A full UUID is 36 characters of noise that pushes the origin — the thing a user is
 * actually here for — out of the header. The first segment is unambiguous enough to tell
 * two owners apart at a glance, and `title` carries the whole thing for anyone who needs to
 * match it against something.
 */
export function ownerShortId(userId: string): string {
  return userId.split("-")[0] ?? userId;
}

export function OwnerLabel({ userId, className }: { userId: string; className?: string }) {
  const { user } = useUser();

  // `user` is `null` for one render while the Supabase session is read out of the cookie,
  // and on the server. Falling back to the short id during that moment — rather than
  // guessing "you" and correcting it — means the label never flickers from a wrong answer
  // to a right one; it only ever gets more specific.
  const isOwner = user !== null && user.id === userId;

  return (
    <span className={cn("text-sm text-muted-foreground", className)} title={userId}>
      {isOwner ? (
        "Added by you"
      ) : (
        <>
          Added by <span className="font-mono text-xs">{ownerShortId(userId)}</span>
        </>
      )}
    </span>
  );
}
