// Thin typed fetch wrapper for BROWSER call sites. Not a component, so no "use client"
// directive — this is a lib module, exporting a class and a function. The `/api` prefix
// this module resolves every path against is relative, so it only means anything in the
// browser, against the page's own origin (`app/api/[...path]/route.ts`); calling it
// server-side would resolve nowhere meaningful. Server code that needs the API talks to
// API_URL directly instead of importing this module.
//
// This file has two halves. `apiFetch<T>` below is the low-level primitive — a caller
// supplies `T` positionally and gets no help from the compiler if it is wrong. The typed
// client at the bottom of this file (search "Typed client") is what every feature-level
// helper (lib/api/websites.ts and friends) should call instead: it derives `T`, the
// request body, and every parameter from the generated `paths` type in `./schema`
// (openapi-typescript, regenerated from the backend's own OpenAPI document — see
// scripts/export-openapi.sh), so an endpoint's shape is declared exactly once. `apiFetch`
// itself stays exported and unchanged for the rare caller that has no `paths` entry to
// lean on (there is none today, but the seam should not disappear).

import type { paths } from "./schema";

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

// Every narrow read of `detail` below works off a body whose shape this module has no
// contract with (it came from `JSON.parse` on whatever the proxy or FastAPI sent back), so
// each guard is a chain of `in`/`typeof` checks and nothing is ever cast: the `in` check
// narrows `body` on its own, so the `typeof` that follows it is checking a property
// TypeScript already agrees exists. `extractErrorMessage` below tries these, most specific
// first, and only falls through to the generic message when none of them match.

// FastAPI's own `HTTPException(status_code=..., detail="...")` shape. Covers `404`s and
// most `401`s/`403`s, and also the BFF proxy's own error body (app/api/[...path]/route.ts's
// `ProxyErrorBody`), which reuses this exact key for the same reason: one field for a
// caller to read regardless of which layer produced the error.
function hasStringDetail(body: unknown): body is { detail: string } {
  return (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof body.detail === "string"
  );
}

// `detail` as a JSON object carrying a human-readable `message` — the shape of
// `WebsiteAlreadyExistsResponse` (backend/app/features/websites/schemas.py): a `409`'s
// `detail` is `{ code, message, website_id, origin }`, not a string. Without this case,
// `hasStringDetail` above misses entirely (the `typeof body.detail === "string"` check
// fails) and the generic fallback throws "Request failed with status 409" — exactly the
// unhelpful message this ticket exists to fix. Checked after `hasStringDetail`, not
// before, so an endpoint that returns a plain string detail keeps getting exactly that.
function hasObjectDetailWithMessage(body: unknown): body is { detail: { message: string } } {
  return (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof body.detail === "object" &&
    body.detail !== null &&
    "message" in body.detail &&
    typeof body.detail.message === "string"
  );
}

// One item of FastAPI's native `422` body, `HTTPValidationError.detail` — a
// `ValidationError[]` (backend/app/features/websites/schemas.py re-exports the generated
// type; the shape itself is pydantic's own). `loc` and `type` exist on every item too, but
// `msg` is the only field worth surfacing to a person, so it is the only one this guard
// requires.
function isValidationErrorItem(value: unknown): value is { msg: string } {
  return (
    typeof value === "object" && value !== null && "msg" in value && typeof value.msg === "string"
  );
}

// A non-empty array of validation errors. Empty on purpose is excluded: pydantic never
// emits a `422` with zero entries, so an empty array here would mean this guard matched
// something that was never actually a `ValidationError[]`, and falling through to the
// generic message is the more honest answer for that shape.
function hasValidationErrorDetail(body: unknown): body is { detail: { msg: string }[] } {
  return (
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    Array.isArray(body.detail) &&
    body.detail.length > 0 &&
    body.detail.every(isValidationErrorItem)
  );
}

// The single place every `ApiError.message` comes from. Order is deliberate: a plain
// string `detail` is both the most common shape and the cheapest check, so it goes first;
// the `409` object shape and the `422` array shape are checked next, in the order a caller
// is likely to hit them; and only a body matching none of the three falls back to a
// message that names nothing but the status code.
function extractErrorMessage(status: number, body: unknown): string {
  if (hasStringDetail(body)) return body.detail;
  if (hasObjectDetailWithMessage(body)) return body.detail.message;
  if (hasValidationErrorDetail(body)) return body.detail.map((item) => item.msg).join("; ");
  return `Request failed with status ${status}`;
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
    throw new ApiError(response.status, body, extractErrorMessage(response.status, body));
  }

  // A 2xx with nothing to read — 204 is the common case, but any empty 2xx body parses to
  // `null` above — has nothing to cast to `T`. Callers ask for this explicitly with
  // `apiFetch<void>`.
  if (body === null) return undefined as T;

  return body as T;
}

// ============================================================================
// Typed client — generic over `paths` from ./schema
// ============================================================================
//
// Everything below builds `api.get`/`api.post`/`api.delete` on top of `apiFetch` above.
// The type-level goal is that an endpoint's request and response shapes are declared
// exactly once, in the backend's Pydantic models — `paths` (openapi-typescript's output
// in `./schema.d.ts`, regenerated from `./openapi.json` by `npm run gen:api`) is the
// single downstream copy, and every call site reads its types from it rather than
// re-declaring them. `frontend/lib/api/schema.type-test.ts` is where that promise is
// checked at compile time: a call with an unknown path, a wrong param type, or a wrong
// body type must fail to build.
//
// This section is deliberately self-contained: nothing above it needs to know a `paths`
// type exists, and nothing outside it should reach into these type helpers directly —
// `lib/api/websites.ts` and friends are the intended call sites, via the exported `api`
// object at the bottom.

// The four verbs every current caller needs. PUT landed with the schedules ticket
// (`PUT /websites/{id}/schedule` upserts a website's schedule) — PATCH is still nothing
// planned. Widening this list further means adding the verb here once, plus one more
// method on the `api` object below — everything else generalizes over it already.
type HttpMethod = "get" | "post" | "put" | "delete";

// The subset of `paths`' keys that actually declare method `M`. openapi-typescript emits
// every unused method on every path as `{ verb?: never }`, so filtering on "does this
// path's `M` resolve to something other than `never`" is what turns `paths` (which has an
// entry, technically, for every verb on every path) into "the paths you can actually call
// this way" — this is also what makes `api.get("/nope")` a compile error rather than a
// runtime 404: "/nope" is not a member of `PathsWithMethod<"get">` at all.
//
// The `infer Op` here has to happen inside a *structural* match (`paths[P] extends { [K in
// M]: infer Op }`), not a bare indexed access (`paths[P][M]`): indexed access on an
// optional property adds `| undefined` to an otherwise-`never` type, and `never |
// undefined` simplifies to plain `undefined` — which is not `never`, and would make every
// unused verb look "present" to the `[Op] extends [never]` check below. Matching
// structurally against the property's own declared type does not add that `undefined`.
type PathsWithMethod<M extends HttpMethod> = {
  [P in keyof paths]: paths[P] extends { [K in M]: infer Op }
    ? [Op] extends [never]
      ? never
      : P
    : never;
}[keyof paths];

// The generated operation object for one verified (path, method) pair — the thing
// `operations["create_website_websites_post"]` names in ./schema.d.ts, reached generically
// instead of by that generated, implementation-detail name.
type OperationFor<P extends keyof paths, M extends HttpMethod> = paths[P] extends {
  [K in M]: infer Op;
}
  ? Op
  : never;

// `never` (not present) for a path with no `{param}` segment, e.g. `/websites`; the exact
// `{ id: string }` shape for `/websites/{id}`.
type PathParamsOf<Op> = Op extends { parameters: { path: infer P } } ? P : never;

// `?: infer Q` — not a bare `infer Q` — matters here for the same reason it does in
// `PathsWithMethod` above: an endpoint with no query parameters declares `query?: never`,
// and matching the optional property structurally keeps that `never` rather than widening
// it to `undefined`. `[Q] extends [undefined]` then catches the "declared but genuinely
// optional-and-absent-shaped" case openapi-typescript emits for endpoints that accept no
// query at all, alongside the "declared `?: never`" case.
type QueryParamsOf<Op> = Op extends { parameters: { query?: infer Q } }
  ? [Q] extends [undefined]
    ? never
    : Q
  : never;

// `never` for a GET/DELETE (no `requestBody` key at all); the request's JSON schema for a
// POST. Only a *required* `requestBody` matches — every write this client makes today
// requires one, and a future optional body would need its own branch here rather than
// silently becoming `never`.
type RequestBodyOf<Op> = Op extends { requestBody: { content: { "application/json": infer B } } }
  ? B
  : never;

// FastAPI only ever returns one 2xx per operation, so this union collapses to exactly one
// member for any real `Op` — `200` for a `GET`, `201` for `POST /websites`, `202` for
// `POST /websites/{id}/runs`, `204` for the `DELETE` — and the `SuccessStatus extends infer
// S` distributes over the candidates, keeping only the one that is actually a key of `R`.
//
// `202` earns its own place in this union rather than being folded in with `201`: PER-160's
// run trigger returns `202 Accepted` deliberately (the run row exists, but the crawl it
// names has only been queued — see `TriggerRunResponse` in
// backend/app/features/runs/schemas.py). Until `202` was listed here, `SuccessResponseOf`
// resolved to `never` for that one operation, and it failed in the worst possible
// direction: `never` is assignable to everything, so a `triggerRun` helper declaring
// `Promise<TriggeredRun>` compiled happily while this client's own inferred response type
// for the call said it returns nothing at all. Any future 2xx the backend introduces needs
// the same one-line entry, and `schema.type-test.ts` is where that stays checked.
type SuccessStatus = 200 | 201 | 202 | 204;
type SuccessResponseOf<Op> = Op extends { responses: infer R }
  ? SuccessStatus extends infer S
    ? S extends keyof R
      ? R[S]
      : never
    : never
  : never;

// A `204`'s response has no `content` key at all (there is no body to describe), which is
// exactly the case this resolves to `void` rather than `never` — `never` would make
// `deleteWebsite`'s return type uncallable, where `void` correctly says "nothing to read."
type JsonBodyOf<Response> = Response extends { content: { "application/json": infer Body } }
  ? Body
  : void;

type ResponseFor<P extends keyof paths, M extends HttpMethod> = JsonBodyOf<
  SuccessResponseOf<OperationFor<P, M>>
>;

// The three optional pieces of a call's second argument, each included in the intersection
// only when the operation actually has one — `object` (not `{}` or `Record<string,
// never>`) as the "nothing to add" case is what makes `object extends RequestOptions<Op>`
// below evaluate to "no required keys" for an operation with none of the three.
type MaybeParams<Op> = [PathParamsOf<Op>] extends [never] ? object : { params: PathParamsOf<Op> };
type MaybeQuery<Op> = [QueryParamsOf<Op>] extends [never]
  ? object
  : { query?: QueryParamsOf<Op> };
type MaybeBody<Op> = [RequestBodyOf<Op>] extends [never] ? object : { body: RequestBodyOf<Op> };

type RequestOptions<Op> = MaybeParams<Op> & MaybeQuery<Op> & MaybeBody<Op>;

// A one-element tuple, present only when at least one of params/body is required —
// `object extends RequestOptions<Op>` is true exactly when every key of `RequestOptions`
// is optional, i.e. an empty object already satisfies it. This is what lets
// `api.get("/websites")` compile with no second argument at all, while
// `api.get("/websites/{id}", ...)` and `api.post("/websites", ...)` require one: a rest
// parameter typed as this tuple is spreadable into a genuinely optional or genuinely
// required parameter depending on which branch `Op` lands in.
type OptionsArg<Op> =
  object extends RequestOptions<Op>
    ? [options?: RequestOptions<Op>]
    : [options: RequestOptions<Op>];

// The one runtime shape every call's `options` argument is narrowed to before use. The
// type-level machinery above exists so a *caller* never writes this shape by hand and
// never sees a param/body it shouldn't be able to pass; internally, by the time a request
// actually goes out, all three pieces have collapsed to "does this exist, plain JS value if
// so" — there is no correctness the compiler is still checking for us past this point, so
// naming the runtime shape once here and casting into it is honest about that rather than
// threading three more generics through `performRequest` to no benefit.
interface RuntimeRequestOptions {
  params?: Record<string, string | number>;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
}

// Every `{name}` segment in an OpenAPI path template, replaced with its caller-supplied
// value. `encodeURIComponent` per segment (not once over the whole resulting path) is what
// keeps a value that itself contains a `/` — unlikely for a UUID, but this has no way to
// know its params are always UUIDs — from being reinterpreted as an extra path separator
// once it reaches `apiFetch`.
function substitutePathParams(path: string, params: Record<string, string | number>): string {
  return path.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    if (!(name in params)) {
      throw new Error(`api: missing path parameter "${name}" for ${path}`);
    }
    return encodeURIComponent(String(params[name]));
  });
}

// `undefined`/`null` values are dropped rather than serialized as the strings "undefined"
// and "null" — the acceptance criterion this exists for is `{ include: undefined }`
// producing no `?include=` at all, not one that means something to no server anywhere.
function buildQueryString(
  query: Record<string, string | number | boolean | null | undefined>
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    search.set(key, String(value));
  }
  const serialized = search.toString();
  return serialized ? `?${serialized}` : "";
}

async function performRequest<T>(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  options: RuntimeRequestOptions | undefined
): Promise<T> {
  const resolvedPath = options?.params ? substitutePathParams(path, options.params) : path;
  const queryString = options?.query ? buildQueryString(options.query) : "";
  const init: RequestInit = { method };
  if (options?.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }
  return apiFetch<T>(`${resolvedPath}${queryString}`, init);
}

/**
 * The typed client. `api.get`, `api.post`, `api.put`, and `api.delete` derive their request
 * and response shapes from `paths` (`./schema.d.ts`), so a call to an endpoint that does not
 * exist, or one that gets a parameter or a body wrong, fails at `tsc`, not at `fetch`.
 * `lib/api/websites.ts`, `lib/api/health.ts`, and every React Query hook in
 * `lib/query/` call through this object rather than `apiFetch` directly.
 */
export const api = {
  get<P extends PathsWithMethod<"get">>(
    path: P,
    ...args: OptionsArg<OperationFor<P, "get">>
  ): Promise<ResponseFor<P, "get">> {
    // The cast below is the one place this file trusts the type-level plumbing above
    // rather than re-proving it at runtime: `args[0]`'s static type is always one of the
    // `Maybe*` shapes composed above, which is a subset of `RuntimeRequestOptions` for
    // every `Op` this client can be instantiated with. There is no `any` here — `args[0]`
    // is soundly typed the whole way down, just not typed identically to the interface
    // `performRequest` needs, which is `unknown`'s usual gap between a generic type and a
    // concrete one.
    const options = args[0] as RuntimeRequestOptions | undefined;
    return performRequest("GET", path, options);
  },

  post<P extends PathsWithMethod<"post">>(
    path: P,
    ...args: OptionsArg<OperationFor<P, "post">>
  ): Promise<ResponseFor<P, "post">> {
    const options = args[0] as RuntimeRequestOptions | undefined;
    return performRequest("POST", path, options);
  },

  put<P extends PathsWithMethod<"put">>(
    path: P,
    ...args: OptionsArg<OperationFor<P, "put">>
  ): Promise<ResponseFor<P, "put">> {
    const options = args[0] as RuntimeRequestOptions | undefined;
    return performRequest("PUT", path, options);
  },

  delete<P extends PathsWithMethod<"delete">>(
    path: P,
    ...args: OptionsArg<OperationFor<P, "delete">>
  ): Promise<ResponseFor<P, "delete">> {
    const options = args[0] as RuntimeRequestOptions | undefined;
    return performRequest("DELETE", path, options);
  },
};
