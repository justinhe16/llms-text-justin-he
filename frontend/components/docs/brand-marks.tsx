/**
 * The eight vendor marks the architecture topology needs that `lucide-react` does not ship
 * (`components/docs/anthropic-mark.tsx` is the ninth, and reused from its existing path —
 * PER-192 does not move it, because moving it would edit `lib/docs/sections.ts` for no
 * behaviour change and enlarge the run tab's diff for a file it does not touch).
 *
 * Every component here follows `AnthropicMark` exactly: the signature is
 * `({ className, ...props }: React.SVGProps<SVGSVGElement>)`, which is Lucide's own icon
 * signature, so `ArchitectureNodeShape["icons"]` can hold a Lucide import, `AnthropicMark`,
 * or any of these without a union at the call site. `fill="currentColor"` and a set
 * `xmlns` on every `<svg>`; no hardcoded `width`/`height`, so the caller's `size-4` sets the
 * rendered size the same way it does for a Lucide icon; no `<title>`, because the button
 * that renders each icon owns the accessible name and every call site marks the icon itself
 * `aria-hidden="true"`.
 *
 * Committed rather than hotlinked, for the reason `anthropic-mark.tsx` already gives: `/docs`
 * (and now `/docs/architecture`) is a public page that makes no external request, and a logo
 * fetched from a CDN would be the only one on it.
 *
 * One file rather than eight, because these are not eight independent decisions — they are
 * one decision ("committed inline SVG, `currentColor`, Lucide's signature") applied eight
 * times, and a shared module docstring says that once instead of once per file. No brand-icon
 * package is installed for the same reason `anthropic-mark.tsx` was written by hand instead
 * of pulling in one for a single glyph: eight marks is still a handful, every one of them is
 * `aria-hidden` and decorative, and a package brings a version to track and a bundle-size
 * line item for icons this page uses once each.
 *
 * ## On fidelity
 *
 * Every mark below is built from the vendor's actual logomark geometry — a triangle for
 * Vercel, a circle with the "N" cut out of it for Next.js, an asymmetric bolt for Supabase, a
 * balloon envelope for Fly, a bolt inside a ring for FastAPI, three stacked plates for Redis's
 * database shape. Two are simplified rather than reproduced stroke-for-stroke: the Postgres
 * elephant and the Python two-snake mark are both too intricate to survive a solid-fill
 * `currentColor` render at 16px without turning into a smudge, and every icon here is
 * `aria-hidden` with the node's visible label carrying the actual name — so a clean,
 * recognizable simplification beats a faithful outline that reads as noise at this size.
 */

export function VercelMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      <path d="M12 2 22.5 20H1.5Z" />
    </svg>
  );
}

export function NextjsMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* A ring (the badge) with the "N" cut out of it as a single hole, evenodd — the same
          technique the real mark uses (a dark circle, an "N" in the page colour). */}
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M12 1A11 11 0 1 0 12 23A11 11 0 1 0 12 1ZM7.5 7L9.5 7L14.5 14L14.5 7L16.5 7L16.5 17L14.5 17L9.5 10L9.5 17L7.5 17Z"
      />
    </svg>
  );
}

export function FlyMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* The balloon envelope, tapering to a point above the basket. */}
      <path d="M12 2A7 7 0 0 1 16.95 13.95L13.8 18H10.2L7.05 13.95A7 7 0 0 1 12 2Z" />
      <rect x="10" y="19.5" width="4" height="2.5" rx="0.5" />
    </svg>
  );
}

export function FastApiMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* A ring with a bolt cut out of it, evenodd — FastAPI's mark is a lightning bolt on a
          teal disc. */}
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M12 1A11 11 0 1 0 12 23A11 11 0 1 0 12 1ZM13 2 3 14H12L11 22 21 10H12Z"
      />
    </svg>
  );
}

export function SupabaseMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* An asymmetric bolt, unringed — Supabase's mark, unlike FastAPI's, is the bolt alone,
          no enclosing circle. */}
      <path d="M13 1 5 14H11L9 23 19 10H13Z" />
    </svg>
  );
}

export function PostgresMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* Simplified: a head, an ear, and a curled trunk — the elephant reduced to its three
          most legible strokes at 16px, per this module's fidelity note above. The ear only
          just meets the head, rather than sinking into it, so the two stay two round shapes
          instead of fusing into one asymmetric blob. */}
      <circle cx="10" cy="11" r="6" />
      <circle cx="16.5" cy="7.5" r="1.8" />
      <path d="M7.5 16.3c0 2-1.2 3.5-3.2 4a.75.75 0 1 0 .4 1.4C7.3 21 9 19 9 16.3Z" />
    </svg>
  );
}

export function RedisMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* Three stacked plates — the generic stacked-cylinder database shape, standing in for
          Redis's own dice-cluster mark, which does not survive a 16px solid fill. */}
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <ellipse cx="12" cy="12" rx="8" ry="3" />
      <ellipse cx="12" cy="19" rx="8" ry="3" />
    </svg>
  );
}

export function PythonMark({ className, ...props }: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      {...props}
    >
      {/* Two point-symmetric bodies about the icon's centre — the two-snake mark reduced to
          its defining shape (two interlocked, rotationally symmetric halves) with no eyes and
          no two-tone colour, per this module's fidelity note above. */}
      <path d="M11 3a5 5 0 0 0-5 5v2H4a1 1 0 0 0-1 1v3a5 5 0 0 0 5 5h2v-3H8a2 2 0 0 1-2-2v-2h7a3 3 0 0 0 3-3V5a2 2 0 0 0-2-2Z" />
      <path d="M13 21a5 5 0 0 0 5-5v-2h2a1 1 0 0 0 1-1v-3a5 5 0 0 0-5-5h-2v3h2a2 2 0 0 1 2 2v2h-7a3 3 0 0 0-3 3v4a2 2 0 0 0 2 2Z" />
    </svg>
  );
}
