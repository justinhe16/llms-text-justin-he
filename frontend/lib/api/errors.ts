// Turns an error response that carries a *structured*, actionable `detail` back into its
// typed shape, so a caller can act on it instead of just displaying it. Three of them so
// far, all built the same way: a `409` from `POST /websites`, and a `409` and a `429` from
// `POST /websites/{id}/runs`.
//
// `WebsiteAlreadyExistsDetail`'s own docstring (backend/app/features/websites/schemas.py)
// is explicit about why the backend bothers: "Pasting a URL you already added is a normal
// thing to do from the landing page, and the useful response is 'you already have this,
// here it is' — the frontend navigates to `website_id` instead of rendering an error the
// user cannot act on." `RunAlreadyInFlightDetail` says the same thing about a run: a `409`
// that hands back nothing actionable makes the client build a second request just to find
// out what it collided with.
//
// This module is those navigations' only entry point — nowhere else in the frontend should
// read `error.body` off an `ApiError` by hand to get at `website_id` or `run_id`.
//
// Note what is deliberately NOT here: a guard for reading a human-readable message off an
// error. `lib/api/fetcher.ts`'s `extractErrorMessage` already unwraps `detail.message` for
// every one of these shapes when it builds `ApiError.message`, so `toast.error(err.message)`
// surfaces the backend's own wording verbatim — including the `429`'s cap message, which
// names the limit and when it resets. A guard below is only warranted when a caller needs a
// *field* (`run_id`, `scope`) to branch or navigate on, never just to display prose.

import { ApiError } from "./fetcher";
import { isRunStatus } from "./run-status";
import type { RunAlreadyInFlightDetail, RunLimitExceededDetail } from "./runs";
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
  const detail = detailOf(error, 409);
  return isWebsiteAlreadyExistsDetail(detail) ? detail : null;
}

// ============================================================================
// POST /websites/{id}/runs — the run trigger's two structured failures
// ============================================================================

/**
 * The `detail` of an `ApiError` whose status is exactly `status`, or `undefined` for
 * anything else: a different status, an `error` that is not an `ApiError` at all (a network
 * failure throws a `TypeError` and never reaches this module), or a body with no `detail`
 * key. Shared by all three guards below so the "is this even the error I think it is" half
 * is written once and the per-shape guards stay pure field checks.
 *
 * Returns `undefined` rather than throwing, for the reason `isWebsiteAlreadyExists`'s
 * docstring gives: a caller here has already caught a real error and is deciding how to
 * react to it, which is not the place to raise a second, different one.
 */
function detailOf(error: unknown, status: number): unknown {
  if (!(error instanceof ApiError) || error.status !== status) return undefined;

  const body = error.body;
  if (typeof body !== "object" || body === null || !("detail" in body)) return undefined;

  return body.detail;
}

// Structural check against the generated type, not a cast — same discipline as
// `isWebsiteAlreadyExistsDetail` above. `status` goes through `isRunStatus`
// (lib/api/run-status.ts) rather than being compared against four inline string literals:
// that predicate is derived from the one exhaustive `Record` this app keeps of the Postgres
// enum, so a fifth `run_status` value can never leave this guard quietly rejecting a body
// the backend considers valid.
function isRunAlreadyInFlightDetail(value: unknown): value is RunAlreadyInFlightDetail {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    value.code === "run_already_in_flight" &&
    "message" in value &&
    typeof value.message === "string" &&
    "run_id" in value &&
    typeof value.run_id === "string" &&
    "status" in value &&
    isRunStatus(value.status)
  );
}

/**
 * Returns the run-trigger `409`'s detail — the id and status of the run already in
 * flight — or `null` if `error` is anything else.
 *
 * This is what turns "you can't start a run" into "here is the run that is already
 * going": the caller (`lib/query/use-trigger-run.ts`) selects `detail.run_id` in the
 * Output tab rather than leaving the user staring at a toast with nothing to click.
 */
export function isRunAlreadyInFlight(error: unknown): RunAlreadyInFlightDetail | null {
  const detail = detailOf(error, 409);
  return isRunAlreadyInFlightDetail(detail) ? detail : null;
}

// `scope`'s two values are spelled out inline here, unlike `status` above, and the
// difference is deliberate. `RunStatus` is app-wide vocabulary with an existing single
// definition to reach for; `scope` is local to this one error shape, appears nowhere else
// in the frontend, and has no `Record` keeping it honest — inventing a module to own two
// strings used once would be ceremony, not safety.
function isRunLimitExceededDetail(value: unknown): value is RunLimitExceededDetail {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    value.code === "run_limit_exceeded" &&
    "message" in value &&
    typeof value.message === "string" &&
    "limit" in value &&
    typeof value.limit === "number" &&
    "scope" in value &&
    (value.scope === "concurrent" || value.scope === "daily") &&
    "resets_at" in value &&
    (value.resets_at === null || typeof value.resets_at === "string")
  );
}

/**
 * Returns the run-trigger `429`'s detail — which cap was hit, its limit, and (daily cap
 * only) when it resets — or `null` if `error` is anything else.
 *
 * Callers should still show `error.message`, not rebuild the sentence from these fields:
 * `lib/api/fetcher.ts` already lifts the backend's own wording onto `ApiError.message`, and
 * that wording is the one the backend's tests pin. This guard exists for the parts of the
 * `429` that are *not* prose — `scope`, so the UI can tell "wait for a run to finish" apart
 * from "come back tomorrow", and `resets_at`, which is a machine-readable instant the
 * message only renders as a rounded-down delay.
 */
export function isRunLimitExceeded(error: unknown): RunLimitExceededDetail | null {
  const detail = detailOf(error, 429);
  return isRunLimitExceededDetail(detail) ? detail : null;
}
