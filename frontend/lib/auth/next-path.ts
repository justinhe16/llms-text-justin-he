// Validates a `next` redirect target so a sign-in flow can never send a user
// off this origin. This closes an open-redirect hole: a crafted callback URL
// like `/auth/callback?next=https://evil.com` (or a protocol-relative
// `//evil.com`) would otherwise be honored verbatim after sign-in.
//
// Rejected shapes (all fall back to DEFAULT_NEXT_PATH):
//   - undefined / null / ""            no value supplied
//   - "https://evil.com"               absolute URL, different origin
//   - "evil.com/path"                  no leading slash — not a relative path
//   - "//evil.com"                     protocol-relative URL (browsers treat
//                                       a leading "//" as "same scheme, new host")
//   - "/\\evil.com"                    a leading "/\" is treated as "//" by
//                                       some URL parsers (browser quirk)
//   - "/a\\b"                          any embedded backslash
//   - "/a\nLocation: evil"             control characters (header/response
//                                       splitting, CR/LF injection)
// Accepted shapes:
//   - "/crawls"
//   - "/crawls/123?tab=runs"

export const DEFAULT_NEXT_PATH = "/crawls";

export function safeNextPath(value: string | null | undefined): string {
  if (!value) return DEFAULT_NEXT_PATH;
  if (!value.startsWith("/")) return DEFAULT_NEXT_PATH;
  if (value.startsWith("//")) return DEFAULT_NEXT_PATH;
  if (value.startsWith("/\\")) return DEFAULT_NEXT_PATH;
  if (value.includes("\\")) return DEFAULT_NEXT_PATH;

  // Scanned with char codes rather than a regex control-character class, to
  // avoid tripping eslint's no-control-regex rule without disabling it.
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return DEFAULT_NEXT_PATH;
  }

  return value;
}
