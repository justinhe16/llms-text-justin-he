// Thin typed fetch wrapper for BROWSER call sites. Not a component, so no "use client"
// directive — this is a lib module, exporting a class and a function. The `/api` prefix
// this module resolves every path against is relative, so it only means anything in the
// browser, against the page's own origin (`app/api/[...path]/route.ts`); calling it
// server-side would resolve nowhere meaningful. Server code that needs the API talks to
// API_URL directly instead of importing this module.
//
// This is also the seam where generated OpenAPI request/response types get bolted on in a
// later ticket: callers already pass `T` positionally to `apiFetch<T>`, so wiring in a
// generated type there changes call sites, not this file.

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// A caller passing a full URL ("https://api.example.com/x") would silently escape the
// same-origin proxy and call out cross-origin from the browser — exactly what
// ARCHITECTURE.md §8.1 says never happens. Rejecting it outright (rather than trying to
// interpret it) is simplest, and keeps this function's contract to "relative path only."
const ABSOLUTE_URL_PATTERN = /^[a-z][a-z0-9+.-]*:\/\//i;

function normalizePath(path: string): string {
  if (ABSOLUTE_URL_PATTERN.test(path) || path.startsWith("//")) {
    throw new Error(
      `apiFetch: expected a path relative to the proxy, got an absolute URL: ${path}`
    );
  }
  const withoutLeadingSlash = path.startsWith("/") ? path.slice(1) : path;
  return `/api/${withoutLeadingSlash}`;
}

// A narrow read of `detail` off a body whose shape this module has no contract with (it
// came from `JSON.parse` on whatever the proxy or FastAPI sent back). No cast of any kind:
// the `in` check narrows `body` on its own, so the `typeof` that follows it is checking a
// property TypeScript already agrees exists.
function hasStringDetail(body: unknown): body is { detail: string } {
  return (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof body.detail === "string"
  );
}

// Parsing must never throw: a response is JSON when its content-type says so, otherwise
// raw text, and malformed JSON despite that header falls back to the text rather than
// blowing up a call site that only wanted to know a request failed.
async function parseResponseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return text;

  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Typed fetch wrapper for browser call sites. Always resolves against the same-origin
 * `/api/[...path]` proxy — never calls Fly directly (ARCHITECTURE.md §8.1) — so `path` is
 * always relative: both "/health" and "health" are accepted and normalized to "/api/health".
 *
 * Only a non-2xx *response* becomes an `ApiError`; a network failure (offline, DNS,
 * connection reset) propagates as whatever native error `fetch` throws — a `TypeError` in
 * every browser — so a caller that wants to handle both has to catch both.
 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const url = normalizePath(path);

  // Caller-supplied headers win: both defaults below are set only when the caller hasn't
  // already supplied that header.
  const headers = new Headers(init?.headers);
  if (!headers.has("accept")) headers.set("accept", "application/json");
  if (init?.body !== undefined && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }

  const response = await fetch(url, { ...init, headers });
  const body = await parseResponseBody(response);

  if (!response.ok) {
    const message = hasStringDetail(body)
      ? body.detail
      : `Request failed with status ${response.status}`;
    throw new ApiError(response.status, body, message);
  }

  // A 2xx with nothing to read — 204 is the common case, but any empty 2xx body parses to
  // `null` above — has nothing to cast to `T`. Callers ask for this explicitly with
  // `apiFetch<void>`.
  if (body === null) return undefined as T;

  return body as T;
}
