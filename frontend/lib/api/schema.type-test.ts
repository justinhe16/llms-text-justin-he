// This file is never imported at runtime — nothing in the app reaches for it, and it does
// not export a single value that does anything at execution time. It exists purely for
// `tsc --noEmit` and `next build` to type-check, which `npm run typecheck`/`npm run build`
// already do, and which `make lint` and `.github/workflows/ci-frontend.yml` already run.
// There is no vitest/jest in this repo — adding one is a cross-cutting tooling decision
// that deserves its own ticket — so this is the only place the claims this ticket makes
// about the typed client (lib/api/fetcher.ts) are checked at all.
//
// Every `@ts-expect-error` below is an assertion, not a suppression, and is the *opposite*
// of the `@ts-ignore` this repo bans (CLAUDE.md): `@ts-expect-error` fails the build if the
// very next line does NOT produce an error, so each one records "this must never compile"
// rather than silencing something that currently does. If a schema change ever makes one
// of these calls valid again — a path un-deleted, a field's type widened back — that is a
// real regression in what the generic client rejects, and this file is what turns it into
// a build failure instead of a silent behavior change.

import type { components } from "./schema";
import { api } from "./fetcher";
import { getHealth, type HealthStatus } from "./health";
import type { RunStatus } from "./run-status";
import { getRun, listRuns } from "./runs";
import type { RunDetail, RunPage } from "./runs";
import { getSchedule, putSchedule } from "./schedules";
import type { Schedule } from "./schedules";
import { createWebsite, deleteWebsite, getWebsite, listWebsites } from "./websites";
import type { Website, WebsiteListItem } from "./websites";

// A type-level "is A exactly B" check, not merely "does A extend B" — the latter would let
// a helper that returns strictly more fields than it should (an accidental extra property,
// a union that is wider than the real one) still pass every assertion below. This is the
// standard trick for exact equality: two conditional types over a fresh, unconstrained type
// parameter `T` are structurally identical only when `A` and `B` are the same type.
type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2
  ? true
  : false;

// Fails to compile unless its argument is literally `true` — this is what turns `Equal<A,
// B>` from "a type that happens to be `false`" into an actual assertion.
type Expect<T extends true> = T;

// --- each named helper (lib/api/*.ts) resolves to the type it claims to -----------------
//
// Exported so eslint's unused-bindings check has nothing to flag: these are never read by
// anything, on purpose, but an unexported `type` alias that nothing references would
// otherwise look exactly like dead code to a linter that cannot tell "unused" from
// "the check IS the file existing."

export type AssertListWebsitesReturnsListItems = Expect<
  Equal<Awaited<ReturnType<typeof listWebsites>>, WebsiteListItem[]>
>;

export type AssertGetWebsiteReturnsWebsite = Expect<
  Equal<Awaited<ReturnType<typeof getWebsite>>, Website>
>;

export type AssertCreateWebsiteReturnsWebsite = Expect<
  Equal<Awaited<ReturnType<typeof createWebsite>>, Website>
>;

// `deleteWebsite` resolves `void`, matching the backend's `204 No Content` — see
// `SuccessBodyOf` in lib/api/fetcher.ts for why a response with no `content` key resolves to
// `void` rather than `never`.
export type AssertDeleteWebsiteReturnsVoid = Expect<
  Equal<Awaited<ReturnType<typeof deleteWebsite>>, void>
>;

export type AssertGetHealthReturnsHealthStatus = Expect<
  Equal<Awaited<ReturnType<typeof getHealth>>, HealthStatus>
>;

export type AssertListRunsReturnsRunPage = Expect<
  Equal<Awaited<ReturnType<typeof listRuns>>, RunPage>
>;

export type AssertGetRunReturnsRunDetail = Expect<
  Equal<Awaited<ReturnType<typeof getRun>>, RunDetail>
>;

// `getSchedule` resolves `Schedule | null`, and the `| null` is the entire point of this
// assertion rather than an incidental detail of it. `GET /websites/{id}/schedule` answers
// `200` with a `null` body when a website simply has no schedule yet — a normal state, not
// an error (`get_schedule` in backend/app/api/routers/schedules.py reserves `404` for an
// unknown *website*). `Equal<>` is exact, so this fails just as loudly if the null is ever
// dropped — leaving call sites to dereference a schedule that isn't there — as it would if
// a non-nullable response were widened for no reason.
export type AssertGetScheduleReturnsNullableSchedule = Expect<
  Equal<Awaited<ReturnType<typeof getSchedule>>, Schedule | null>
>;

// `putSchedule`, by contrast, is NOT nullable: the upsert always returns the schedule it
// just wrote, so a caller never has to null-check the thing it just set.
export type AssertPutScheduleReturnsSchedule = Expect<
  Equal<Awaited<ReturnType<typeof putSchedule>>, Schedule>
>;

// --- the two text/plain artifact endpoints resolve to `string`, not `void` ---------------
//
// PER-181's acceptance criterion for the `SuccessBodyOf` change in lib/api/fetcher.ts, and
// the only thing standing between "these operations return text" and the silent
// `Promise<void>` that the `application/json`-only version produced. Asserted through
// `api.get` directly rather than through a named helper because there is deliberately no
// `lib/api/runs.ts` helper for these yet — the frontend download button is its own ticket —
// so this is the call the eventual helper will be built on.
//
// `Equal<>` is exact, so this fails just as loudly if the fix is ever over-applied and the
// branch starts swallowing a JSON response as `string`.
export type AssertLlmsTxtDownloadResolvesToText = Expect<
  Equal<Awaited<ReturnType<typeof api.get<"/runs/{id}/llms.txt">>>, string>
>;

export type AssertLlmsFullTxtDownloadResolvesToText = Expect<
  Equal<Awaited<ReturnType<typeof api.get<"/runs/{id}/llms-full.txt">>>, string>
>;

// The regression guard in the other direction: adding a `text/plain` branch must not have
// changed what a JSON operation resolves to. `AssertDeleteWebsiteReturnsVoid` above covers
// the third branch (a 204 with no `content` key at all) for the same reason.
export type AssertGetRunStillResolvesToJson = Expect<
  Equal<Awaited<ReturnType<typeof api.get<"/runs/{id}">>>, RunDetail>
>;

// --- RunStatus is derived from the generated schema, not hand-copied --------------------
//
// This is the assertion `lib/api/run-status.ts`'s own comment promises: if a fifth
// `run_status` value is ever added to the Postgres enum and flows through to
// `RunListItemResponse.status`, `RunStatus` (a direct alias of that field, via
// `RunListItem["status"]`) picks it up the next time `npm run gen:api` runs — this check
// only exists to catch the alias itself ever drifting from its source, e.g. someone
// "simplifying" it to a hand-written union later.

export type AssertRunStatusMatchesGeneratedEnum = Expect<
  Equal<RunStatus, components["schemas"]["RunListItemResponse"]["status"]>
>;

// The runs feature's own status field (`RunListItemResponse.status`, the definition now
// backing `RunStatus` above) and the websites feature's folded summary of it
// (`LatestRunSummary.status`, `GET /websites?include=latest_run`) are two independently
// generated fields that are both supposed to trace back to the single `RunStatusName`
// Literal the runs feature owns (`backend/app/features/runs/schemas.py`'s module docstring
// explains why there is only one copy of it, and why `websites/schemas.py` imports rather
// than redefines it). This assertion is the compile-time guarantee that the two have not
// quietly drifted apart: if the runs feature ever grows a fifth status the websites fold
// does not know about, this fails `tsc`, rather than `websiteHasActiveRun` and `runIsActive`
// (lib/api/run-status.ts) silently answering "is this active" two different ways for what
// was supposed to be one vocabulary.
export type AssertWebsiteRunStatusMatchesRunsRunStatus = Expect<
  Equal<
    components["schemas"]["LatestRunSummary"]["status"],
    components["schemas"]["RunListItemResponse"]["status"]
  >
>;

// --- the generic client rejects what it should -------------------------------------------

async function assertInvalidCallsDoNotCompile(): Promise<void> {
  // @ts-expect-error "/nope" is not a key of the generated `paths` type
  await api.get("/nope");

  // @ts-expect-error the "/websites/{id}" path parameter is a string, not a number
  await api.get("/websites/{id}", { params: { id: 123 } });

  // @ts-expect-error "/websites/{id}" has a required path parameter and cannot be called bare
  await api.get("/websites/{id}");

  // @ts-expect-error CreateWebsiteRequest.url is a string, not a number
  await api.post("/websites", { body: { url: 123 } });

  // @ts-expect-error POST /websites has a required body and cannot be called without one
  await api.post("/websites");

  // @ts-expect-error GET /health declares no DELETE method
  await api.delete("/health");

  // @ts-expect-error the "/websites/{id}/runs" path parameter is a string, not a number
  await api.get("/websites/{id}/runs", { params: { id: 123 } });

  // @ts-expect-error the "/runs/{id}/llms.txt" path parameter is a string, not a number
  await api.get("/runs/{id}/llms.txt", { params: { id: 123 } });

  // @ts-expect-error "queued" is not a member of RunStatusName
  await api.get("/websites/{id}/runs", { params: { id: "abc" }, query: { status: "queued" } });

  // The two schedule cases below assert through the named helper (`putSchedule`) rather
  // than `api.put` directly, on two counts. It is the stronger claim — "calling a helper
  // with a wrong argument type is a compile error" is the acceptance criterion, and the
  // helper is what call sites actually use. And it keeps each call to a SINGLE LINE, which
  // this file structurally requires: `@ts-expect-error` suppresses errors on the one line
  // immediately after it, so a multi-line call whose offending property sits three lines
  // down leaves the directive inert — reported by tsc as an unused directive — while the
  // real error escapes underneath it. Every assertion in this function is one line, and the
  // directive is always the last comment line before the code. Keep it that way.

  // 90 is not one of the four `ScheduleIntervalMinutes` presets. The backend's
  // `Literal[60, 360, 1440, 10080]` (schedules/schemas.py) renders as an OpenAPI enum, and
  // this is where a value outside it stops compiling rather than only failing a runtime 422.
  // @ts-expect-error interval_minutes 90 is outside the four supported presets
  await putSchedule("abc", { active: true, interval_minutes: 90 });

  // `UpsertScheduleRequest` requires both fields — there is no partial update on this
  // endpoint (see that type's own docstring for why) — so a body missing one cannot compile.
  // @ts-expect-error interval_minutes is required and is missing from this body
  await putSchedule("abc", { active: true });
}

// Referenced so this is "used" for lint purposes without ever actually being called —
// calling it would just await a network request that has no server behind it in this
// context, and doing so buys nothing: every check above already happened at compile time.
void assertInvalidCallsDoNotCompile;
