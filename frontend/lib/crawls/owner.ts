// Who added a website, as far as the frontend can currently tell.
//
// THE GAP THIS FILE WORKS AROUND, STATED PLAINLY: `GET /websites?include=latest_run`
// returns `user_id` — a bare UUID — and nothing else about the owner. There is no `/users`
// endpoint in lib/api/openapi.json, and `lib/auth/use-user.ts` reads GitHub handle and
// avatar out of the *signed-in* user's own Supabase session, which says nothing about
// anybody else. So this table can render a real handle for exactly one person: you.
//
// Two things it deliberately does NOT do. It does not call Supabase from the browser to
// look other users up — the frontend never talks to anything but the BFF proxy
// (ARCHITECTURE.md §8.1), and the Auth admin API is a service-role credential that belongs
// nowhere near this bundle. And for anybody else, it does not dress the UUID prefix up as
// `@something`: a fabricated handle that looks like a GitHub handle but is not one is worse
// than an honest short id, because it is the kind of thing someone copies into a search box.
// PER-195 renders *your own* real handle and avatar on your own row, read from your own
// session via `lib/auth/use-user.ts` — the opposite of fabricating one, since it is an
// identity the frontend genuinely has. The prohibition above was always about inventing an
// identity for someone the frontend cannot see, not about the one identity it can.
//
// Closing the gap properly means the backend returning an owner handle/avatar on the list
// response, which is a schema change and therefore its own ticket. When it lands, this file
// is the only thing that changes.

import { initials } from "@/lib/auth/initials";
import type { AuthUser } from "@/lib/auth/use-user";

/** The rendered identity of a website's owner. */
export interface OwnerIdentity {
  /** True when the signed-in user added this website. Drives both the "you" label and, on
   * the detail page, which controls are live — though this table renders no controls. */
  isYou: boolean;
  /** What the Owner column prints. */
  label: string;
  /** The full `user_id`, for the tooltip — the only complete, unambiguous thing we have. */
  userId: string;
}

/** Enough of the UUID to tell two owners apart at a glance, and short enough not to break
 * the column. Eight hex characters is ~4 billion values; a collision inside one page of
 * this table is not a real concern, and the tooltip carries the full id regardless. */
const SHORT_ID_LENGTH = 8;

function shortId(userId: string): string {
  return userId.replace(/-/g, "").slice(0, SHORT_ID_LENGTH);
}

/**
 * `currentUserId` is `null` while `useUser()` is still resolving the session, or if there
 * is somehow no session at all. Every row then renders as somebody else's, which is the
 * right way round to be wrong for the half-second it lasts: labelling a stranger's row
 * "you" is a claim about ownership, and labelling your own row with its short id is only
 * less friendly.
 */
export function ownerIdentity(userId: string, currentUserId: string | null): OwnerIdentity {
  const isYou = currentUserId !== null && currentUserId === userId;
  const short = shortId(userId);
  return {
    isYou,
    label: isYou ? "you" : short,
    userId,
  };
}

/** The fields of `AuthUser` (lib/auth/use-user.ts) this module reads. Structural, so
 * lib/crawls/ depends on four named fields rather than on the whole auth shape. */
export type OwnerViewer = Pick<AuthUser, "id" | "handle" | "avatarUrl" | "displayName">;

/** What the Owner pill renders. Exactly one of `avatarUrl`/`initial` is non-null on your
 * own row; both are null on everybody else's — their pill is the short id alone. */
export interface OwnerPill {
  /** True only when the signed-in user added this website. False while the session is
   * still resolving, for the reason `ownerIdentity` documents. */
  isYou: boolean;
  /** The pill's own text: "you", or the eight-character short id. */
  text: string;
  /** Your own GitHub handle, `@`-prefixed, when the session carries one. `null` on every
   * other row, and on yours if you signed in without one (the dev password path). */
  handle: string | null;
  /** The full `user_id`. The tooltip whenever `handle` is null, and the only complete,
   * unambiguous thing this frontend has for anybody else. */
  userId: string;
  /** Your own GitHub avatar. `null` on every other row and while the session resolves. */
  avatarUrl: string | null;
  /** The letter drawn in place of a missing avatar on your own row. */
  initial: string | null;
}

export function ownerPill(userId: string, viewer: OwnerViewer | null): OwnerPill {
  const { isYou, label } = ownerIdentity(userId, viewer?.id ?? null);

  if (!isYou || viewer === null) {
    return { isYou: false, text: label, handle: null, userId, avatarUrl: null, initial: null };
  }

  return {
    isYou: true,
    text: label,
    handle: viewer.handle === null ? null : `@${viewer.handle}`,
    userId,
    avatarUrl: viewer.avatarUrl,
    initial: viewer.avatarUrl === null ? initials(viewer.displayName) : null,
  };
}
