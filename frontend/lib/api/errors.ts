// Turns the `409` from `POST /websites` (lib/api/websites.ts's `createWebsite`) back into
// its typed detail, so a caller can act on it instead of just displaying it.
//
// `WebsiteAlreadyExistsDetail`'s own docstring (backend/app/features/websites/schemas.py)
// is explicit about why the backend bothers: "Pasting a URL you already added is a normal
// thing to do from the landing page, and the useful response is 'you already have this,
// here it is' — the frontend navigates to `website_id` instead of rendering an error the
// user cannot act on." This module is that navigation's only entry point — nowhere else in
// the frontend should read `error.body` off an `ApiError` by hand to get at `website_id`.

import { ApiError } from "./fetcher";
import type { WebsiteAlreadyExistsDetail } from "./websites";

// Structural check against the generated type, not a cast: every field
// `WebsiteAlreadyExistsDetail` declares is checked, including the literal `code`
// discriminator — ARCHITECTURE.md's own comment on that field says a caller should branch
// on `code`, never on the prose in `message`, and this guard is what makes that possible
// without trusting an arbitrary JSON body's shape.
function isWebsiteAlreadyExistsDetail(value: unknown): value is WebsiteAlreadyExistsDetail {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    value.code === "website_already_exists" &&
    "message" in value &&
    typeof value.message === "string" &&
    "website_id" in value &&
    typeof value.website_id === "string" &&
    "origin" in value &&
    typeof value.origin === "string"
  );
}

/**
 * Returns the `409`'s detail if `error` is one, `null` for everything else — a wrong
 * status, an `error` that is not even an `ApiError` (a network failure never reaches this
 * far), or a body that does not match the shape `WebsiteAlreadyExistsResponse` promises.
 * That last case should never happen against a backend generated from the same schema this
 * type comes from, and `null` rather than a thrown error is the correct response to it
 * regardless: a caller here already caught a real error and is deciding how to react to
 * it, which is not the place to raise a second, different one.
 */
export function isWebsiteAlreadyExists(error: unknown): WebsiteAlreadyExistsDetail | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;

  const body = error.body;
  if (typeof body !== "object" || body === null || !("detail" in body)) return null;

  return isWebsiteAlreadyExistsDetail(body.detail) ? body.detail : null;
}
