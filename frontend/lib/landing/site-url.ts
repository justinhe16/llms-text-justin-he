// Client-side validation for the landing page's one input.
//
// This is a *pure* function with no I/O, which is why it lives in `lib/landing/` rather
// than inside the field component (ARCHITECTURE.md §8.4): "anything in `lib/<feature>/`
// should be readable without knowing what the screen looks like".
//
// It is deliberately NOT a second copy of the backend's normalization. `POST /websites`
// owns what an origin is — lowercasing the scheme and host, dropping the default port and
// the path — and that answer is what deduplicates a website. Re-deriving it here would give
// the UI a second opinion that could disagree with the row that actually gets written. All
// this does is answer the one question worth answering before spending a round trip: is
// what the user typed an absolute http(s) URL at all?
//
// It also does not silently rewrite the input. Prepending `https://` to a bare `example.com`
// would be helpful right up until it guesses wrong about a host that is only served over
// plain http, and it makes the field's contents differ from what gets sent. Saying so and
// letting the user fix it is one keystroke and no ambiguity.

/** `POST /websites`'s `CreateWebsiteRequest.url` declares `maxLength: 2048`. Caught here so
 *  an over-long paste is an inline sentence rather than a 422 with a Pydantic message in it. */
const MAX_URL_LENGTH = 2048;

export type SiteUrlResult =
  | { ok: true; url: string }
  | { ok: false; message: string };

/**
 * Validates what the user typed, returning either the trimmed URL to submit or the message
 * to render under the field.
 *
 * Accepted: any absolute `http:` or `https:` URL with a host — `https://example.com`,
 * `http://localhost:3000/docs`, `https://example.com/a?b=c`.
 *
 * Rejected, each with its own message, because "Invalid URL" tells a user nothing about
 * which of the four things they got wrong.
 */
export function parseSiteUrl(input: string): SiteUrlResult {
  const trimmed = input.trim();

  if (trimmed.length === 0) {
    return { ok: false, message: "Enter a website URL." };
  }
  if (trimmed.length > MAX_URL_LENGTH) {
    return { ok: false, message: `That URL is longer than ${MAX_URL_LENGTH} characters.` };
  }

  let parsed: URL;
  try {
    // The one-argument form, with no base: it throws on anything that is not already
    // absolute, which is exactly the check being made. Passing a base would happily resolve
    // `example.com` against the current page and accept it.
    parsed = new URL(trimmed);
  } catch {
    return { ok: false, message: "Enter a full URL, including https://" };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { ok: false, message: "Only http:// and https:// URLs can be crawled." };
  }

  // `new URL("https://")` throws, but `new URL("file:///x")`-shaped inputs and a few
  // others parse with an empty host. A URL with no host is not a website.
  if (parsed.hostname.length === 0) {
    return { ok: false, message: "That URL has no domain in it." };
  }

  return { ok: true, url: trimmed };
}
