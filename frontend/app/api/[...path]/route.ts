// Backend-for-frontend catch-all proxy. ARCHITECTURE.md §8.1: "the browser never calls
// Fly" — every request a browser makes to the FastAPI service goes through this file
// instead, forwarded server-side. This is also the one place a Supabase access token is
// read and attached to an outbound request (§8.2: "the only place a token is deliberately
// handled, and it happens entirely on the server").
//
// `app/auth/callback/route.ts` is not shadowed by this catch-all: Next matches the most
// specific route segment first, and `/auth/callback` never falls under `/api`. A future
// genuine Next-native API route needs the same property — anything more specific than
// `app/api/[...path]/` wins over this file, so add it alongside, not instead of, this one.

import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

type RouteContext = { params: Promise<{ path: string[] }> };

// Bounds how long this handler waits on Fly. This is not FastAPI's own timeout — it exists
// so a wedged upstream becomes a clean, typed 504 from this proxy instead of whatever
// generic error Vercel's own function-execution budget produces when it kills the
// function out from under an in-flight fetch.
const UPSTREAM_TIMEOUT_MS = 30_000;

// One documented shape for every error this proxy *itself* generates — as opposed to an
// upstream 4xx/5xx, whose body is forwarded verbatim (see the pass-through comment below).
// `detail` deliberately matches FastAPI's own error key, so the UI has exactly one field to
// read for both a proxy-origin and an upstream-origin error. `error` is the stable,
// machine-readable discriminator for a caller that needs to branch on failure kind.
type ProxyErrorCode =
  | "not_authenticated"
  | "proxy_misconfigured"
  | "invalid_path"
  | "invalid_body"
  | "upstream_unreachable"
  | "upstream_timeout";

type ProxyErrorBody = { error: ProxyErrorCode; detail: string };

function proxyErrorResponse(status: number, error: ProxyErrorCode, detail: string): Response {
  const body: ProxyErrorBody = { error, detail };
  return NextResponse.json(body, { status });
}

// The Fetch spec forbids a body on these statuses — `new Response(x, { status })` throws
// if `x` is non-null for any of them — so the upstream response has to be replayed as
// bodyless regardless of what (if anything) Fly sent.
const BODYLESS_UPSTREAM_STATUSES = new Set([204, 205, 304]);

function isTimeoutError(error: unknown): boolean {
  if (error instanceof DOMException) return error.name === "TimeoutError";
  return error instanceof Error && error.name === "TimeoutError";
}

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  // --- a. Authenticate, server-side, before any round trip to Fly. -----------------------

  const supabase = await createClient();

  // getClaims(), never getSession(), makes the authorization decision: it verifies the JWT
  // (locally against the JWKS when the project uses asymmetric signing keys) and refreshes
  // an about-to-expire session first. getSession() would hand back whatever the cookie
  // holds without revalidating it.
  //
  // frontend/middleware.ts already ran getClaims() for this same request (its matcher
  // covers /api), so this is the second verification on the path. That repetition is
  // deliberate rather than wasteful: middleware's job is refreshing the session cookie,
  // and a route handler that inferred "middleware let this through" would be trusting a
  // matcher config to be an authorization check. With asymmetric signing keys the second
  // call verifies locally against an already-cached JWKS, so it costs no network round
  // trip. This handler decides for itself whether the caller is signed in.
  let isAuthenticated: boolean;
  try {
    // `data` is the wrapper `{ claims, header, signature }`, not the claims payload itself,
    // so the check is `data?.claims` — the same shape lib/supabase/middleware.ts reads.
    // Destructuring `data` as `claims` here would still be correct today and would mean
    // something else entirely to the next person who writes `claims.sub`.
    const { data, error } = await supabase.auth.getClaims();
    isAuthenticated = Boolean(data?.claims) && !error;
  } catch (error) {
    // Reached when the Auth server can't be talked to at all (DNS, timeout, an outage).
    // Treated as "not authenticated" rather than a 502: from the caller's side there is no
    // verified session either way, and a single failure mode (401) is simpler to document
    // and handle client-side than a second one that means almost the same thing. Only the
    // message is logged — never the error payload, and never a token (ARCHITECTURE.md §9.4).
    console.error(
      "api proxy: could not verify the session —",
      error instanceof Error ? error.message : "unknown error"
    );
    return proxyErrorResponse(401, "not_authenticated", "Authentication required.");
  }
  if (!isAuthenticated) {
    return proxyErrorResponse(401, "not_authenticated", "Authentication required.");
  }

  // Only now read the session, and only to obtain the token to forward — the decision
  // above has already been made and the refresh above has already run, so this reads the
  // current token out of memory rather than trusting a cookie.
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) {
    return proxyErrorResponse(401, "not_authenticated", "Authentication required.");
  }

  // --- b. Resolve API_URL lazily, inside the handler. -------------------------------------

  // Never at module scope: the frontend CI job (.github/workflows/ci-frontend.yml) does not
  // set API_URL, and a module-scope read-and-throw would take `next build` down for every
  // route in the app, not just this one.
  const apiUrl = process.env.API_URL;
  if (!apiUrl) {
    // The variable's NAME only, never a value — there is no value to log anyway.
    console.error("api proxy: API_URL is not configured");
    return proxyErrorResponse(500, "proxy_misconfigured", "The API is not configured.");
  }

  // --- c. Build the target URL. ------------------------------------------------------------

  const { path } = await context.params;
  const base = apiUrl.replace(/\/+$/, "");
  // Next hands `path` back already URL-decoded, so a segment that itself contained an
  // encoded "/" (`%2F`) has already been split apart by the time it reaches this handler.
  // Re-encoding each segment before rejoining is what stops that from turning into an
  // extra path separator once it reaches the upstream.
  const segments = path.map(encodeURIComponent).join("/");
  const target = new URL(`${base}/${segments}${request.nextUrl.search}`);

  // Containment guard: `new URL()` collapses `..` path segments per the URL spec, so a
  // `path` element of `..` can walk the resolved URL back above `base` (e.g. above an
  // `API_URL` that itself carries a path prefix). Comparing the fully-resolved `href`
  // against `base` after the fact catches that cheaply, without writing a path parser.
  //
  // The comparison is against `${base}/`, not `base`, because a bare `startsWith(base)` is
  // a string test rather than a path test: with a `base` of ".../api", a resolved
  // ".../api-internal" shares the prefix and would sail through. The trailing slash forces
  // the match onto a path-segment boundary. `href === base` is allowed separately — that is
  // the base itself, which is contained by definition, not an escape from it.
  if (target.href !== base && !target.href.startsWith(`${base}/`)) {
    return proxyErrorResponse(400, "invalid_path", "Invalid request path.");
  }

  // --- d. Forward the request. --------------------------------------------------------------

  const outgoingHeaders = new Headers();
  // Allowlist, not blind forwarding. In particular, never `cookie`: it carries the Supabase
  // session cookie, and FastAPI has no business seeing it — the bearer token below is the
  // only credential it needs. Never `host` or `connection` either (they describe this hop,
  // not the one to Fly), and never `content-length` (fetch recomputes it from whatever body
  // is actually sent below).
  const contentType = request.headers.get("content-type");
  if (contentType) outgoingHeaders.set("content-type", contentType);
  const accept = request.headers.get("accept");
  if (accept) outgoingHeaders.set("accept", accept);
  outgoingHeaders.set("authorization", `Bearer ${session.access_token}`);

  // Buffered, not streamed. Piping `request.body` straight into `fetch` needs
  // `duplex: "half"`, which is not part of the DOM `RequestInit` type — using it would mean
  // an `any`/`@ts-ignore` escape hatch, which this repo bans outright. Every request body
  // this proxy carries is small JSON, so buffering costs nothing that matters here. GET is
  // the only method among the five exported below that Next ever routes without a body.
  let outgoingBody: ArrayBuffer | undefined;
  if (request.method !== "GET") {
    try {
      const buffered = await request.arrayBuffer();
      outgoingBody = buffered.byteLength > 0 ? buffered : undefined;
    } catch {
      // Reading the incoming body can fail on its own — a client that disconnects
      // mid-upload, a malformed transfer-encoding. Catching it keeps that answerable in
      // this file's own error shape instead of falling through to Next's generic error
      // page, which is the one response here a UI could not parse.
      return proxyErrorResponse(400, "invalid_body", "Could not read the request body.");
    }
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers: outgoingHeaders,
      body: outgoingBody,
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (error) {
    // Never log `target` — it is built from API_URL, which is server-only — and never log
    // headers. The message alone is enough to diagnose a proxy failure from here.
    console.error(
      "api proxy: upstream request failed —",
      error instanceof Error ? error.message : "unknown error"
    );
    if (isTimeoutError(error)) {
      return proxyErrorResponse(504, "upstream_timeout", "The upstream service timed out.");
    }
    return proxyErrorResponse(502, "upstream_unreachable", "The upstream service is unreachable.");
  }

  // --- e. Return the upstream response. ------------------------------------------------------

  // Upstream 4xx/5xx pass through verbatim, status and body both — a 409 with an existing
  // website id, for instance, is meaningful to the UI and must never be reshaped into one
  // of the ProxyErrorBody shapes above.
  if (BODYLESS_UPSTREAM_STATUSES.has(upstream.status)) {
    return new Response(null, { status: upstream.status });
  }

  const responseHeaders = new Headers();
  const upstreamContentType = upstream.headers.get("content-type");
  if (upstreamContentType) responseHeaders.set("content-type", upstreamContentType);
  // Deliberately not forwarding content-length or content-encoding: undici transparently
  // decompresses the upstream body before this code ever sees it, so re-emitting
  // `content-encoding: gzip` alongside already-decompressed bytes corrupts the response in
  // the browser, and a stale `content-length` (computed for the compressed body) does the
  // same thing a different way.
  //
  // An allowlist rather than a denylist is also what keeps `set-cookie` from crossing this
  // boundary. Fly has no business setting a cookie on this app's origin — the session here
  // belongs to Supabase and is written by @supabase/ssr alone — and a denylist would hand
  // it that power the first time someone forgot to add a header to the list.

  // Streamed, not buffered — `upstream.body` is handed straight to `Response` so that
  // adding a streaming endpoint later (e.g. run progress) needs no rewrite here.
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export async function GET(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function POST(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function PUT(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext): Promise<Response> {
  return proxy(request, context);
}
