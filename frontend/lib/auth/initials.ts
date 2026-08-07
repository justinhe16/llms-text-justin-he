// A single-letter fallback for a missing avatar image.

/**
 * The first character of `displayName`, uppercased, for the monogram drawn in place of an
 * avatar image.
 *
 * `"?"` for an empty string — `AuthUser.displayName` (lib/auth/use-user.ts) is never actually
 * empty, so that branch is defensive only, not a real code path.
 */
export function initials(displayName: string): string {
  const trimmed = displayName.trim();
  return trimmed ? trimmed.slice(0, 1).toUpperCase() : "?";
}
