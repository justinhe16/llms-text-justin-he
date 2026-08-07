# Architecture

This document is the engineering contract for `llms-text`. It was written before any
application code existed, so that the tickets that follow produce one coherent codebase
instead of one dialect per pull request.

**Precedence.** Every ticket in this project references this document. If a ticket
contradicts it, **this document wins** and the ticket should be corrected. If a pattern
here turns out to be wrong, change it here first — in its own PR — and then change the
code. Do not let the two drift.

**Vocabulary.** A **website** is a domain a user has registered for crawling. A **run** is
one crawl of a website. A run produces an **llms.txt** artifact, stored as the `llms_txt`
field. Those three nouns are used consistently in the database, the API, and the backend —
tables, columns, routes, services, readers, and writers all say `website` and `run`, never
`crawl`.

The one deliberate exception is the frontend directory `components/crawls/` (§8.4), which
groups the user-facing UI for this product area. "Crawl" is a UI-level category name there
and nowhere else; it is not a schema noun, not a route segment, and not a service name. Do
not let it leak back into the API or the database — `/websites/{id}/runs` is a run, and the
table is `runs`.

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Repo layout](#2-repo-layout)
3. [Backend architecture](#3-backend-architecture)
4. [The authorization contract — public read, owner write](#4-the-authorization-contract--public-read-owner-write)
5. [Transaction boundaries](#5-transaction-boundaries)
6. [Database and migration policy](#6-database-and-migration-policy)
7. [Deploy policy](#7-deploy-policy)
8. [Frontend conventions](#8-frontend-conventions)
9. [Secrets hygiene](#9-secrets-hygiene)
10. [Naming conventions](#10-naming-conventions)
11. [Out of scope](#11-out-of-scope)

---

## 1. System overview

Four moving parts, three deploy targets:

```
                       ┌──────────────────────────────┐
      Browser ───────► │  Next.js — Vercel            │
                       │  App Router                  │
                       │  app/api/[...path]/  (BFF)   │
                       └──────────────┬───────────────┘
                                      │  server-side fetch, API_URL
                                      │  (the browser never calls Fly)
                                      ▼
                       ┌──────────────────────────────┐
                       │  Fly.io — one image          │
                       │  ┌────────────┬────────────┐ │
                       │  │  FastAPI   │ ARQ worker │ │
                       │  │  (app)     │ (worker)   │ │
                       │  └────────────┴────────────┘ │
                       └────────┬─────────────┬───────┘
                  asyncpg/HTTP  │             │  job queue
                                ▼             ▼
                  ┌───────────────────┐  ┌──────────────┐
                  │  Supabase         │  │  Redis       │
                  │  Postgres / Auth  │  │  (ARQ)       │
                  │  Storage          │  └──────────────┘
                  └───────────────────┘
```

Postgres is reached over **asyncpg**; Supabase **Auth and Storage are ordinary HTTPS calls**,
not database traffic. That distinction matters in §5.1: a Storage upload is a network call and
therefore happens outside any transaction.

| Piece | Runtime | Deployed to | Notes |
| --- | --- | --- | --- |
| `frontend/` | Next.js (App Router) | Vercel | Vercel project root directory is `frontend/` |
| `backend/` | FastAPI + ARQ | Fly.io | One image, two processes (`app`, `worker`) |
| `db/` | Prisma CLI | — | Schema and migration tooling only; never runs in production |
| Postgres / Auth / Storage | Supabase | — | Backend talks to Postgres over **asyncpg** |
| Job queue | Redis (Upstash) | — | Required by ARQ; `rediss://` in production, plain `redis://` locally |

**On the name `app`.** The two Fly process groups are `app` and `worker`, declared in
[`backend/fly.toml`](./backend/fly.toml). Earlier revisions of this document called the
first one `web`, which read better but was never what production ran: a fly.toml with no
`[processes]` block puts every machine in Fly's implicit group, and that group is called
`app`. By the time process groups were declared explicitly, machines had been running under
that name since the deploy pipeline landed, and flyctl destroys machines whose group is no
longer declared — so `web` would have cost a teardown and rebuild of the running fleet to
buy a nicer word. `app` is therefore the name, everywhere, permanently.

Two consequences fall out of this shape and are load-bearing everywhere else in the
document:

- **The browser never talks to Fly.** All frontend requests go to Next.js route handlers,
  which forward to FastAPI server-side. There is therefore **no CORS configuration
  anywhere in this repo**, and none is to be added.
- **The frontend never talks to Postgres.** Not through Prisma Client, not through the
  Supabase JS client's data APIs, not through a direct connection string. Data reaches the
  browser only by way of FastAPI.

---

## 2. Repo layout

```
llms-text-justin-he/
├── frontend/            Next.js → Vercel (Vercel root dir = frontend/)
├── backend/             FastAPI + ARQ worker → Fly.io (one image, two processes)
│   └── app/core/auth/   JWKS cache, JWT verification dependencies, require_owner (§4.2)
├── db/                  schema.prisma + migrations/ (Prisma CLI lives here)
├── supabase/            local Supabase stack config (config.toml, seed.sql)
├── scripts/             shell helpers used by the Makefile (dev.sh, local-env.sh)
├── docker-compose.yml   local Redis only — Supabase is managed by its own CLI
├── Makefile             local dev commands — see CLAUDE.md "Commands"
├── .github/workflows/   path-filtered CI + deploy
├── ARCHITECTURE.md      this file — the engineering contract
├── CLAUDE.md            pointer file for coding agents
└── README.md            what this is, how to run it, deploy policy
```

Rules for the layout:

- **Three documents at the repo root, no `docs/` directory.** Three files is the right size
  for this project. Do not add a fourth document without deleting or folding in another.
- **Each top-level directory owns its own toolchain.** `frontend/` owns `package.json` and
  `tsconfig.json`; `backend/` owns `pyproject.toml`; `db/` owns the Prisma CLI dependency.
  There is no root-level package manifest and no monorepo tool (no workspaces, no Turborepo,
  no Nx). The root `Makefile` and `docker-compose.yml` do not change this: they orchestrate
  the toolchains that already exist in each directory (`pip` in `backend/`, `npm` in `db/`,
  the Supabase CLI) rather than introducing one of their own.
- **CI is path-filtered.** A frontend-only change must not run the backend test suite, and
  vice versa. The workflows live in `.github/workflows/`, and the filtering happens in a
  `changes` job rather than in a `paths:` trigger, so that the required status check on
  `main` still reports on a pull request that touches neither stack. Filtering at the
  trigger would make every docs-only pull request permanently unmergeable. See
  [`README.md`](./README.md#ci).

---

## 3. Backend architecture

### 3.1 Feature module shape

Every feature lives in `backend/app/features/{feature}/` and has the same three-layer shape:

```
backend/app/features/websites/
├── schemas.py              Pydantic DTOs (request/response shapes)
├── service.py              Business logic, transaction boundaries
└── internals/
    ├── websites_reader.py  All SELECTs
    ├── websites_writer.py  All INSERT/UPDATE/DELETE
    └── url_normalize.py    Feature-owned pure logic (no I/O)
```

`internals/` may also hold a feature's own **pure** helpers, as `url_normalize.py` does
above — logic that is neither a DTO nor a query, with no I/O and no state. It stays private
to the feature like everything else in that directory; if a second feature needs it, promote
it to a shared location rather than importing across the boundary or copying it.

Route handlers live separately, in `backend/app/api/routers/{feature}.py`.

| Layer | File | Owns | Must never |
| --- | --- | --- | --- |
| Router | `api/routers/{feature}.py` | Parse input, call one service method, return the response | Contain business logic, SQL, or `pool.execute` |
| Schemas | `features/{feature}/schemas.py` | Pydantic request/response DTOs | Contain logic or DB access |
| Service | `features/{feature}/service.py` | Business logic, ownership checks, transaction boundaries | Write raw SQL |
| Reader | `features/{feature}/internals/{feature}_reader.py` | Every `SELECT` for the feature | Mutate anything |
| Writer | `features/{feature}/internals/{feature}_writer.py` | Every `INSERT` / `UPDATE` / `DELETE` | Commit, or open a transaction |

`internals/` means what it says: readers and writers are private to their feature. A service
may call only its own feature's reader and writer. If feature A needs data owned by feature
B, it calls **B's service**, never B's reader. This is the rule that keeps the dependency
graph acyclic.

**Import direction is one-way:** `api` → `features` → `infrastructure` → `core`. Nothing in
`core` may import from `api`, `features`, or `infrastructure`, and no feature may import
another feature's `internals/`. Import absolutely — `from app.core.settings import settings`,
never `from ..core import settings`.

The first feature module to land becomes the reference implementation for every one after
it. Read it before writing the second. If it drifts from this document, fix the module — not
the document.

### 3.2 Routers are thin

A route handler parses input, calls the service, and returns the response. That is all.

```python
# backend/app/api/routers/websites.py

@router.post("/websites", response_model=WebsiteResponse, status_code=201)
async def create_website(
    body: CreateWebsiteRequest,
    user: CurrentUser = Depends(get_current_user),
    service: WebsiteService = Depends(get_website_service),
) -> WebsiteResponse:
    return await service.create_website(body, user.id)
```

If a router grows an `if`, a `for`, a SQL string, or a second service call that has to
succeed or fail together, that logic belongs in the service.

### 3.3 The worker

The ARQ worker runs from the **same image** as the API, as a second Fly process. It imports
and calls the same services the API does — a background job is a service call with a
different trigger, not a parallel implementation. Job functions live in
`backend/app/worker/`, stay thin for exactly the reasons routers do, and enqueue with typed
arguments only (ids and primitives, never ORM objects or Pydantic models).

```
backend/app/worker/
├── settings.py   WorkerSettings — what `arq app.worker.settings.WorkerSettings` loads
├── policy.py     the numbers: poll delay, timeouts, the retry ladder, the reaper thresholds
└── jobs.py       the job functions themselves
```

`policy.py` exists because the stuck-run reaper's staleness threshold is **derived** from
`job_timeout`, and the job that applies it is imported by `settings.py` — leaving the constant
in `settings.py` would have made that derivation a circular import, and restating it in
`jobs.py` with a "keep these in sync" comment is the drift the reaper cleans up after. Every
setting arq reads is still **assigned directly** in the `WorkerSettings` class body; only the
values on the right-hand side moved.

**The API enqueues; the worker consumes. Neither does the other.** The API opens an
`ArqRedis` pool in its lifespan and exposes it as a dependency; the worker gets its own
connection from arq. Nothing in `app/api/` or `app/features/` imports `app/worker/` — the
dependency runs the other way, so the queue never enters a request path.

Six properties of `WorkerSettings` are load-bearing enough to state here, because each
fails silently rather than loudly:

- **`poll_delay = 5`, not arq's 0.5.** An idle worker issues one Redis command per poll, and
  Upstash bills per command: 0.5s is 172,800 commands a day to do nothing. This is a cost
  decision, not a tuning preference, and it is asserted in a test.
- **`functions` is never empty.** arq refuses to construct a `Worker` with no registered
  functions, so an "empty until the crawl task lands" worker does not idle — it crash-loops
  on Fly with no HTTP listener to fail a health check. A no-op job holds the place.
- **`job_completion_wait` is non-zero.** It is the only thing that makes SIGTERM drain
  rather than cancel, and it has to nest inside `fly.toml`'s `kill_timeout`.
- **`cron_jobs` registers two jobs: the schedule tick every minute and the stuck-run reaper
  every five, both with `max_tries=1`.** A tick that raises is not re-run against the state
  it half-left; the next one re-evaluates from the database. Both are places where more than
  one worker machine racing the same work is a real scenario rather than a theoretical one,
  and the correctness of both rests on `FOR UPDATE SKIP LOCKED` — `SchedulesReader.lock_due`
  and `RunsReader.lock_reapable` — not on arq's best-effort `unique=True`. For the reaper,
  `SKIP LOCKED` is doing a second job as well: it scans a table crawl workers lock rows in
  constantly, and a plain `FOR UPDATE` would queue behind whichever write it happened to meet.
- **`job_timeout` (600s) sits ABOVE the crawl's own wall-clock cap (300s), not below it.**
  The application-level cap is what should end a long crawl, with a message saying so; arq's
  cancellation is the outer backstop for a job wedged somewhere that cap does not cover. It is
  deliberately **not** ordered against `job_completion_wait`: Fly caps `kill_timeout` at 300s,
  so a drain budget can never contain a job timeout set above a 300s crawl cap, and the two
  constraints are unsatisfiable together. See `app/worker/policy.py`, which argues this at
  length, and `tests/test_worker_settings.py`, which asserts the replacement invariants.
- **`max_tries` is arq's ceiling, not the retry policy.** The policy is counted against
  `runs.attempts`, incremented by `claim_pending` inside the atomic claim, because that is the
  only counter a reaper in another process can read. arq's `job_try` resets whenever a run is
  re-enqueued under a new job id; the run's own counter does not.

Startup and shutdown hooks (`on_startup`/`on_shutdown`) open and close the asyncpg pool with
**the same factory the API's lifespan uses**, and publish shared resources on arq's context
dict so jobs build them once per process rather than once per job. The crawl task's shared
`httpx.AsyncClient` (`ctx["http_client"]`, built once by
`app.features.crawl.http_client.build_crawl_client` and closed on shutdown) is exactly that
pattern in use, not merely anticipated by it.

### 3.4 The crawler seam

Real crawling logic — deciding what to do with a fetched, now-parsed page: which to keep,
how to rank or summarize them, anything that calls a model — is **out of scope for this
milestone** and has not been designed yet. Extraction is not: `internals/extract.py` parses
every fetched page's HTML now (PER-177), and `CrawledPage` carries its output. What remains
out of scope, and sits behind one function, is everything downstream of a fetched, parsed
page:

```python
def generate_llms_txt(pages: list[CrawledPage]) -> str:
    ...
```

`CrawledPage`, not `Page`: `app.core.pagination.Page` already names the generic pagination
envelope returned by `GET /websites/{id}/runs`, and a second, unrelated `Page` in the same
codebase is an import collision waiting to happen. The rename changes nothing about the
seam's shape — one argument, a list of fetched pages, returns `str` — only the element
type's name.

Build against that signature. Do not scatter crawling, parsing, or LLM-calling logic
through the services in anticipation of a design that does not exist yet, and do not widen
the signature without a ticket that redesigns this seam.

**Extraction is wired into the fetch path, and `CrawledPage` carries its output.**
`internals/extract.py` parses a page's HTML into a title, a description, and a markdown body
(`extract_content(html, *, url) -> ExtractedContent`, built on trafilatura). It is a pure
function in the same category as `internals/payload.py` and `internals/run_stats.py` — no
I/O, no clock, no network — and it landed unwired, reviewed and tested as a pure module in
its own right, before PER-177 called it: `internals/fetcher.py`'s `fetch_page` now calls
`extract_content` once, inline, right after a response's body is read within budget, whenever
the response's `Content-Type` looks like HTML (`_looks_like_html` — permissive on an absent
or unparseable header, because `extract_content` never raises and an absent header is not
evidence the body is not HTML; an explicit non-HTML type still skips the parse). `CrawledPage`
gains three fields — `description`, `markdown`, and `is_empty`, appended in that order after
`content_bytes` so no positional construction is silently reordered — and its long-reserved
`title` is finally populated rather than always `None`; all four are copied straight across
from `ExtractedContent`. `content` is unchanged, still the undecoded-beyond-transport response
body, kept alongside `markdown` as the run's archival record of what the server actually
sent, distinct from what this feature's own pass made of it. `generate_llms_txt` is still the
stub described above: extraction feeds the seam's input type, it is not the seam.

Landing extraction as an unwired module first, and wiring it in a second, separate ticket,
was deliberate rather than an accident of scheduling. Extraction was the one part of the
undesigned pipeline whose quality could be judged on its own — against fixtures from the
documentation generators this crawler actually meets (Docusaurus, Mintlify, VitePress,
Nextra), asserting that a page's prose survives and its navbar, sidebar, table of contents,
pagination and footer do not — reviewable in isolation instead of buried inside a behaviour
change to the fetch path. Summarization and anything that calls a model remain undesigned and
out of scope; ranking now exists too, but only the kind that happens *before* a fetch — see
the next exception.

`CrawledPage.is_empty` — `ExtractedContent.is_empty`, copied across unchanged — is
instrumentation, not a branch. It is set when a page yields less than `MIN_BODY_CHARS` of
body — the signature of a JavaScript-rendered shell that returned a mount div and a bundle.
Whether to pay for headless rendering is an open question with a real cost attached, and this
flag exists to measure how often it would matter, not to decide it: it is counted once per
run, as `runs.stats["pages_empty_content"]` at `RUN_STATS_VERSION` 2
(`internals/crawler.py`, `internals/run_stats.py`). Nothing branches on it — in particular,
`CrawledPage.title` is never nulled because a page's `is_empty` is `True`, even for the
JavaScript-shell case that flag exists to measure: a shell is real HTML with a real
`<title>`, `extract_content` deliberately keeps it, and discarding it here would be exactly
the branch this paragraph forbids.

**A second exception, still unwired: `internals/url_ranking.py`.** Unlike `extract.py` above,
nothing calls this one yet. `select_urls(candidates, *, seed_url, limit) -> SelectionResult`
turns the URLs a discovery step found into the subset worth spending a run's page budget on.
It normalizes every candidate, drops the ones structurally not worth fetching under named,
individually-counted rules (dated archives, `/tag/` and `/author/` taxonomies, pagination,
feeds, changelogs, off-origin links, and localized duplicates of a page already chosen),
scores the rest on path shape plus the sitemap's own `<priority>` and `<lastmod>`, and takes
the top `limit`. Pure and clock-free like `extract.py`, `payload.py`, and `run_stats.py`, and
**called by nothing today**: `crawl_site`'s `extra_urls` is still an empty tuple, and wiring
discovery and selection into it is PER-176.

The distinction that matters is *where* that ranking sits. It runs entirely **before** any
page is fetched, so it ranks on URL shape and sitemap metadata only — "fetch it and see
whether it was worth fetching" is explicitly not a pass this module may grow, because that
would put content judgement upstream of the seam instead of behind it. How *fetched* pages
are ranked, summarized, or chosen for the artifact is still undesigned and still lives behind
`generate_llms_txt`.

`select_urls` is deterministic by construction: survivors sort by `(-score, url)`, so the URL
tie-break makes a selection a pure function of *which* URLs were discovered rather than of
the order a sitemap or a set of racing fetches happened to yield them in. That is the
upstream half of the guarantee `internals/llms_txt.py`'s own docstring makes for the artifact.
Its per-rule `SelectionResult.dropped` counters reconcile exactly — `discovered_count ==
len(selected) + sum(dropped.values())`, every candidate either selected or dropped under
exactly one rule — and recording them in `runs.stats` belongs to PER-176, not here.

**The bounded execution shell around that seam** lives in `backend/app/features/crawl/`,
which owns no table and therefore holds no reader/writer pair — a feature with private,
table-free I/O to do may keep it in `internals/` anyway, as this one keeps `ssrf.py`,
`fetcher.py`, and `crawler.py`. Every fetch, seed or redirect, passes `internals/ssrf.py`'s
SSRF guard before a socket opens, and the crawl loop (`internals/crawler.py`) runs under six
hard caps read from `Settings` — page count, wall-clock budget, total response bytes,
per-request timeout, concurrency, and a politeness delay between request starts. Hitting one
of those caps ends the crawl with whatever pages it already collected and is a **success**,
not a failure; only the seed itself failing to fetch is treated as one, because a run with no
pages at all has nothing to build an artifact from.

### 3.5 The database infrastructure layer

`backend/app/infrastructure/db/` holds the pieces every feature's reader, writer, and
service build on, so no feature reimplements them: `pool.py` (the asyncpg pool factory
and process-wide singleton), `base_repository.py` (`Reader`/`Writer` base classes that
convert `asyncpg.Record` to `dict[str, Any]` so a `Record` never escapes a repository),
and `transaction.py` (the `transaction()` context manager services use to open a unit of
work — §5). It has no feature-specific logic and no schema knowledge; a feature's reader
and writer subclass `Reader`/`Writer`, and its service calls `transaction()`.

### 3.6 The queue infrastructure layer

`backend/app/infrastructure/queue/pool.py` is the Redis counterpart, and is deliberately the
same shape as `db/pool.py`: a pure factory plus an `open`/`get`/`close` process-wide
singleton, so there is one pattern to learn for both backing services. It holds no job
logic and knows nothing about crawls.

It owns one decision that must not be made anywhere else: **whether a connection is TLS is
derived from the URL scheme, never hardcoded.** Production is Upstash over `rediss://` and
local development is a container over plain `redis://`, so a hardcoded `ssl=True` breaks
`make dev` and a hardcoded `ssl=False` puts the Upstash password on the wire in cleartext.
The same function also turns on the hostname verification arq leaves off by default, so
there is no route to an unverified TLS connection from this codebase.

### 3.7 The storage infrastructure layer

`backend/app/infrastructure/storage/supabase_storage.py` is the third backing-service
package, alongside `db/` (§3.5) and `queue/` (§3.6): a pure settings-to-client factory
(`build_storage_client`) plus a thin wrapper around Supabase Storage's REST upload endpoint
(`SupabaseStorage`, built by `build_supabase_storage`). It holds no crawl logic and knows
nothing about runs or websites — it uploads bytes to a path and returns where they landed.

**Deliberately no `open`/`get`/`close` process-wide singleton, unlike `db/` and `queue/`.**
Both of those exist because FastAPI's request path reaches for a pool built once at API
startup, through a dependency, shared across requests that did not create it. Nothing under
`app/api/` ever calls Storage — only `app.features.crawl.service.CrawlService`, built fresh
per arq job by `app.worker.jobs.crawl_task` from resources `open_worker_resources`
(`app/worker/settings.py`) already put on that job's `ctx`. There is no second caller and no
dependency-injection path for a singleton to serve, so this package does not build one; the
worker's own `ctx` dict already does the "build once per process" job a singleton here would
duplicate.

**The bucket is private, and its layout is website-scoped.** Every crawl run's raw payload is
uploaded to `crawl-payloads/{website_id}/{run_id}.jsonl.gz` — gzip-compressed JSONL, one
fetched page per line (`app.features.crawl.internals.payload`). The bucket name itself is
configurable (`Settings.supabase_storage_bucket`, default `crawl-payloads`), which is why
`SupabaseStorage.upload` returns the bucket-qualified path (`f"{bucket}/{object_path}"`)
rather than the bare object path — the value stored in `runs.storage_path` is self-describing
regardless of which bucket a deployment configures. The website-scoped prefix is load-bearing
for the same reason it is documented in `internals/payload.py`: it is what would make "delete
everything a website ever produced" a single prefix-delete operation, the day that operation
exists (§11 records that it does not yet).

### 3.8 Logging and correlation ids

**`fly logs` is the entire observability story, and that is a decision rather than a gap.**
There is **no error-tracking service on either side of this system** — no Sentry, no
equivalent, nothing like it in `backend/requirements.txt` or under `frontend/`. Backend
errors surface as JSON `ERROR` lines in `fly logs`; frontend errors surface in Vercel's
runtime logs. Adding an error-tracking service later is its own ticket and its own decision.
It is not something a ticket re-introduces in passing, and a later ticket whose prose
mentions one does not override this paragraph.

Two consequences follow, and both are enforced in code rather than asked for politely:

- **An `ERROR` line _is_ the incident record.** No second system holds a copy, so an
  unhandled exception logs its **complete** traceback alongside its correlation id.
  Truncating tracebacks to keep logs tidy would leave nothing to debug from.
- **Every line has to be machine-readable**, because reading an interleaved stream by eye is
  not a debugging strategy for a job system.

`backend/app/core/logging.py` configures both processes. Every line is one JSON object on
stdout carrying `ts` (ISO-8601 UTC), `level`, `logger`, `message`, and `process` — `app` or
`worker`, matching `backend/fly.toml`'s `[processes]` keys — plus whatever the call site
passed as `extra=`. `configure_logging()` is called once per process: from
`app.main.create_app()` for the API, and at module import in `app/worker/settings.py` for the
worker, which never imports `app.main`. It also pulls uvicorn's own loggers onto the same
handler, so nothing the container prints is un-parseable. arq's half of that cannot be done
from Python at import time — its CLI applies its own `dictConfig` afterwards — and is passed
on the command line instead, as `--custom-log-dict app.worker.settings.ARQ_LOG_CONFIG`, in
both `fly.toml` and `scripts/dev.sh`.

**Correlation ids travel in `contextvars`, never in module globals and never as function
arguments.** The API's middleware (`app/api/middleware/request_context.py`) accepts or
generates an `X-Request-ID`, binds it, and echoes it on every response.
`app.worker.jobs.crawl_task` binds `run_id` for the life of one crawl, and `schedule_tick`
binds a `tick_id` for one cron tick. Everything logged beneath those scopes is tagged
automatically — no service, reader, or `internals/` module is handed an id — so

```
fly logs --app llms-text-justin-he | jq 'select(.run_id == "…")'
```

reconstructs one crawl out of a stream shared with every other job. A module-level "current
run id" would be shared by every task on the event loop, so two concurrent crawls
(`max_jobs = 2`) would attribute each other's failures, which is worse than having no
correlation id at all. `backend/tests/test_logging.py` proves that isolation under real
concurrency rather than asserting it in a comment.

The API additionally logs **one summary line per request** — method, path, status,
`duration_ms`, and `user_id` when the request authenticated — at `ERROR` for 5xx, `WARNING`
for the 4xx that mean something actually went wrong (409, 429), `INFO` otherwise, and `DEBUG`
for `/health`, which Fly probes every ten seconds forever. The path never includes the query
string, and nothing on that path reads the `Authorization` header.

Finally, **a redaction filter runs on every record** before any handler formats it, rewriting
bearer tokens, JWTs, credentialed connection strings, and named-credential assignments to
`[REDACTED]`. It is insurance, not permission: §9.4 still forbids logging a secret, and a
call site that does is still a bug — one the filter may not recognise.

---

## 4. The authorization contract — public read, owner write

**This project is not multi-tenant.** There is one flat pool of websites and runs, and every
signed-in user can read all of it. There is no `tenant_id`, no tenant-scoped reader, no
`X-Tenant-ID` header, and no tenant validation helper anywhere in this codebase. If you are
porting a pattern from a multi-tenant project, leave that machinery behind.

The contract has exactly two halves, and it fits on one line:
**reads never filter by user; writes always call `require_owner`.**

**Reads — authenticated, unscoped.**
Read endpoints require a valid Supabase JWT, and **do not** filter by `user_id`. Any
signed-in user sees every website and every run, including `llms_txt` content.

**Writes — authenticated and owned.**
`POST`, `PATCH`, `PUT`, and `DELETE` require a valid JWT **and** ownership of the resource,
checked by `require_owner` (§4.2). A non-owner gets `403`, not `404`.

The reference implementation of both halves is `backend/app/features/websites/` — its
service, reader, and router were written to be copied by the features that follow.

### 4.1 Reads must not be scoped

This is the single easiest rule in this document to break by being helpful. Do not add an
owner filter to a read query. Not to be safe, not for symmetry with the write path, not
because every other codebase you have seen does it.

```python
# CORRECT — reads are unscoped
async def list_websites(self) -> list[WebsiteResponse]:
    return await self._reader.list_all()

# WRONG — never scope a read by owner
async def list_websites(self, user_id: UUID) -> list[WebsiteResponse]:
    return await self._reader.list_for_user(user_id)
```

```sql
-- CORRECT
SELECT id, domain, user_id, created_at FROM websites ORDER BY created_at DESC;

-- WRONG
SELECT id, domain, user_id, created_at FROM websites WHERE user_id = $1;
```

A reader may still accept a `user_id` **as a query argument for a genuinely user-filtered
feature** — "show me my websites" is a legitimate product feature. What is prohibited is
applying that filter to the general read path as an implicit security measure. If a filter
is there, it is because the endpoint's contract says it filters, not because someone
assumed reads should be private.

### 4.2 Ownership is checked in one place

Ownership is checked with a single shared helper, which lives in
**`backend/app/core/auth/ownership.py`** and is the only place in the codebase that compares
an owner to a caller:

```python
from app.core.auth.ownership import require_owner

require_owner(resource, user_id)  # raises 403 if resource.user_id != user_id
```

It takes the **resource**, not `resource.user_id`: a helper taking two bare UUIDs is one
transposition away from comparing a value to itself and authorizing everything. `user_id` is
the caller as a `UUID` — the `CurrentUserId` dependency from `app.api.deps`, which is
`CurrentUser`'s `sub` claim parsed to the type every owner column in this schema decodes to.
Comparing the raw `str` claim against a `uuid.UUID` from Postgres is never equal, which
would return `403` to the resource's actual owner on every request.

It is called **as soon as the resource is in hand, and before any other work** — before any
mutation, before any transaction is opened, and before any external call. Fetching the
resource is the only thing allowed to precede it, because you cannot check an owner without
the row:

```python
async def delete_website(self, website_id: UUID, user_id: UUID) -> None:
    website = await self.get_website(website_id)         # 404 if missing
    require_owner(website, user_id)                      # 403 if not owner
    async with transaction(self._pool) as tx:
        await WebsitesWriter(tx).delete(website_id)
```

Note the writer is **constructed around the transaction** — `WebsitesWriter(tx)` — rather
than handed one per call. A writer is bound to exactly one `DbHandle` for its lifetime, so
there is no way to call a write method and forget to pass the transaction, and no way for
two statements in the same `async with` to land on different connections.

Never scatter ownership checks inline in routers, and never reimplement the comparison. One
helper, called from services, is what makes "is this endpoint authorized?" answerable by
reading a single line.

The review test for a write path is mechanical: find the `require_owner` call, and check that
nothing above it does anything but fetch the resource.

### 4.3 Where authorization lives

Authorization is enforced in the **service layer**, in application code. The backend
connects to Postgres as one application role; it does not rely on Postgres row-level
security to enforce ownership. Do not add RLS policies as a second, divergent source of
truth for who may write what.

---

## 5. Transaction boundaries

**Transactions belong to the service layer.** A service opens a transaction with an async
`transaction()` context manager, which commits on success and rolls back on any exception:

```python
async def record_run_result(self, run_id: UUID, storage_url: str, llms_txt: str) -> None:
    async with transaction(self._pool) as tx:
        writer = RunsWriter(tx)
        await writer.update_status(run_id, status="succeeded")
        await writer.set_output(run_id, storage_url, llms_txt)
```

`transaction()` is a module-level helper taking the pool, not a service method; the writer
is constructed around the handle it writes through. Binding one `RunsWriter` to `tx` and
reusing it is what keeps both statements provably inside the same unit of work.

Rules:

- **Writers never commit.** A writer executes its statement against the connection or
  transaction it is handed, and returns. It does not commit, does not roll back, and does
  not open a transaction of its own. That is what makes writers composable inside a larger
  unit of work.
- **One transaction per unit of work.** If two writes must both land or neither, they go in
  one `transaction()` block. Do not chain separate transactions and hope.
- **Never nest transactions** to work around a layering problem. If you need one, the
  boundary is in the wrong place.

### 5.1 External calls happen outside transactions

**Never hold a database transaction open across a network call.** Supabase Storage uploads,
HTTP fetches, and LLM calls all happen *before* the transaction opens.

Do the external work first, then open a transaction to record the result:

```python
# CORRECT — upload first, then a short transaction to record the outcome
storage_url = await self._storage.upload(key, content)     # network, no transaction held
async with transaction(self._pool) as tx:
    await RunsWriter(tx).set_output(run_id, storage_url, llms_txt)

# WRONG — a slow upload holds a Postgres transaction open the whole time
async with transaction(self._pool) as tx:
    storage_url = await self._storage.upload(key, content)
    await RunsWriter(tx).set_output(run_id, storage_url, llms_txt)
```

This is no longer only illustrative. `app.features.crawl.service.CrawlService.execute_run`
is the real implementation of the CORRECT shape above: it uploads a run's gzip-compressed
JSONL payload to Supabase Storage (`app.infrastructure.storage.supabase_storage`), and only
after that upload has returned does it call `RunService.record_success`, which opens the one
short `transaction()` that writes `llms_txt`, `storage_path`, `stats`, and `completed_at`.
Read that method for what this section looks like once a ticket actually builds it, rather
than treating the snippet above as a hypothetical.

The tradeoff is deliberate: the wrong version risks connection-pool exhaustion and lock
contention under load, and it makes every remote timeout a database problem. The correct
version risks an orphaned storage object if the transaction fails afterwards. Orphaned
objects are cheap and cleanable. Exhausted pools take production down.

---

## 6. Database and migration policy

`db/schema.prisma` is the **single source of truth** for the database schema. The Prisma CLI
lives in `db/` and is used for schema authoring and migrations only.

### 6.1 Prisma Client is not a runtime dependency

Prisma is a **schema and migration tool** in this project, and nothing else.

- The backend reads and writes with **asyncpg**, using hand-written SQL in readers and
  writers.
- Prisma Client is not imported at runtime, in either stack.
- The frontend does not touch the database at all.

### 6.2 How to change the schema

1. Edit `db/schema.prisma`.
2. Generate the migration **without applying it**:
   `prisma migrate dev --create-only`
3. **Read the generated SQL.** Check it for destructive operations, missing indexes, and
   locks that would be taken on a large table.
4. Commit the migration directory together with the `schema.prisma` change, in the same PR.
5. CI applies it with `prisma migrate deploy` — before the application deploy (§7).

### 6.3 Prohibitions

These are prohibitions, not preferences. A PR that violates one does not get merged.

- **Never run `prisma db push`.** Not locally, not against a branch database, not "just to
  try something." It mutates a database without producing a migration, which desynchronizes
  the schema from its history.
- **Never hand-apply SQL to the production database.** Not through the Supabase SQL editor,
  not through `psql`. Production schema changes arrive through `prisma migrate deploy` in
  CI, and by no other route.
- **Never edit a migration that has already been applied.** Write a new migration. Editing
  an applied migration breaks checksum validation and leaves every environment on a
  different schema than the one recorded in the repo.
- **Never commit a schema change without its migration**, or a migration without its schema
  change. They land together or not at all.
- **Never ship a migration that the release before it cannot survive.** Deploy is
  `migrate → deploy` (§7), so there is always a window in which the old image runs against
  the new schema — and if the deploy job fails, that window stays open until someone closes
  it. Migrations are therefore additive: add a column; do not rename or drop one in the same
  release that stops using it. Drop it one release later, once nothing that could reference
  it is still running. This is also the rule that makes the emergency image rollback in §7
  possible at all — **code can be rolled back; a migration cannot.**

### 6.4 The schema

Three tables, and they are the whole application state:

```
             ┌───────────────────────────────┐
             │ websites                      │
             │ ───────────────────────────── │
             │ id            uuid  PK        │
             │ user_id       uuid  NOT NULL  │──▶ auth.users(id), by convention only
             │ url           text            │    (no FK — see below)
             │ origin        text            │
             │ title         text NULL       │
             │ created_at    timestamptz     │
             │ UNIQUE (user_id, origin)      │
             └───────────────────────────────┘
                    │ 1                │ 1
       ON DELETE    │                  │    ON DELETE CASCADE
        CASCADE     │ 0..1             │ 0..n
                    ▼                  ▼
   ┌────────────────────────────┐   ┌──────────────────────────────┐
   │ schedules                  │   │ runs                         │
   │ ────────────────────────── │   │ ──────────────────────────── │
   │ id           uuid  PK      │   │ id           uuid  PK        │
   │ website_id   uuid  UNIQUE  │   │ website_id   uuid  NOT NULL  │
   │ active       bool          │   │ schedule_id  uuid  NULL      │
   │ interval_minutes  int      │◀──│ trigger      run_trigger     │
   │ next_run_at  timestamptz?  │ 0..n  status     run_status      │
   │ last_run_at  timestamptz?  │   │ started_at   timestamptz     │
   │ auto_publish bool          │   │ completed_at timestamptz?    │
   │              (reserved)    │   │ llms_txt     text?           │
   └────────────────────────────┘   │ stats        jsonb?          │
        ON DELETE SET NULL ─────────│ error        text?           │
        (run history outlives       │ storage_path text?           │
         the schedule)              └──────────────────────────────┘
```

**`websites`** — a site a user added. `url` is what they typed; `origin` is the normalized
scheme + host and is the dedupe key. `UNIQUE (user_id, origin)` stops one user adding the
same site twice while letting two users each add it — it is a dedupe key, not a tenancy
boundary (§4).

**`schedules`** — the recurring-run configuration, at most one per website, enforced by a
`UNIQUE` on `website_id` rather than by convention. `(active, next_run_at)` is indexed
because the cron tick reads exactly that pair on every wake-up. `auto_publish` is
**reserved and unused in this milestone**: no code reads or writes it, and it exists so
that enabling publish-on-success later is a behaviour change rather than a migration on a
table the tick is actively scanning.

**`runs`** — one generation attempt and its outcome. `(website_id, started_at DESC)` serves
both the history list and the latest-run lookup. A second, **partial** index covers
`status` where it is `pending` or `processing`: both the duplicate-run guard on a manual
trigger and the stuck-run reaper scan for exactly those two, while completed and failed
rows are the ones that accumulate forever. That index is hand-written in the migration —
Prisma has no syntax for a partial index, and its introspection cannot see one, so it is
deliberately **not** declared in `schema.prisma` (declaring it makes every later
`migrate dev` emit a duplicate `CREATE INDEX`).

`attempts` and `claimed_at` are the retry policy's and the reaper's two columns (PER-166).
`attempts` counts claims, not enqueues: it is incremented by `claim_pending` inside the atomic
`pending -> processing` UPDATE, so it measures attempts that actually STARTED and cannot be
inflated by a redelivered job that lost the claim race. It is never decremented — a run handed
back to the queue keeps the attempt it spent, which is what bounds retrying. `claimed_at`
stamps the same UPDATE and is cleared when a run is returned to `pending`.

**`claimed_at`, and not `started_at`, is the reaper's staleness clock**, which is the one
detail here worth stating rather than discovering. `started_at` is set at INSERT — when the run
was enqueued — and with `max_jobs = 2` against a batch of scheduled runs a row can sit
`pending` for an hour before a worker touches it. A reaper measuring from `started_at` would
reap such a run moments after it was finally claimed, producing a duplicate crawl, which is the
one outcome the threshold exists to avoid. The scan reads `COALESCE(claimed_at, started_at)` so
that rows claimed before the column existed stay reachable.

`llms_txt`, `storage_path`, `stats`, and `completed_at` are no longer merely reserved columns
waiting for a later ticket — `RunService.record_success` writes all four on every successful
run (§5.1), and `record_failure` writes `completed_at`, `error`, and whatever partial `stats`
a crawl produced on every unsuccessful one. `ON DELETE CASCADE` from `websites` reclaims a
website's `runs` rows the moment the website is deleted, but it reclaims nothing in Storage:
the payload each completed row's `storage_path` names keeps existing in the `crawl-payloads`
bucket, orphaned, until something else removes it. Nothing in this milestone does — see §11.

Both `trigger` and `status` are real Postgres enums rather than text with a `CHECK`. The
worker writes status transitions directly, and a typo should fail at the database instead
of persisting a value no reader knows how to interpret. Adding a value later means
`ALTER TYPE ... ADD VALUE`, which deserves its own migration: below Postgres 12 it cannot
run inside a transaction at all, and from 12 on the new value cannot be *used* until the
adding transaction commits.

Every timestamp is `timestamptz`. In a system whose entire purpose is scheduling, a naive
timestamp is a bug waiting for the next DST transition.

#### Why `interval_minutes` and not cron

Schedules store an integer number of minutes. The UI presets — hourly, 6-hourly, daily,
weekly — are a presentation concern that maps to 60 / 360 / 1440 / 10080.

Cron would buy expressiveness nobody has asked for, and charge for it three times: a cron
parser to depend on and keep correct, timezone semantics to define for every stored
expression, and a materially harder "when does this next run?" computation. With an
interval, that computation is `last_run_at + interval`, the cron tick is one indexed
range scan over `(active, next_run_at)`, and "every 90 minutes" — which cron cannot say
without enumerating — is just `90`. If a user ever genuinely needs "weekdays at 09:00",
that is a ticket, not a field nobody planned for.

#### Why there is no foreign key to `auth.users`

`websites.user_id` holds an `auth.users(id)` value and there is no database constraint
saying so. That is deliberate, and it is not a shortcut:

- `auth` is **Supabase's** schema, not ours. Prisma cannot model it without `multiSchema`,
  and pointing Prisma at `auth` invites it to generate `DROP`s for objects Supabase owns.
- The same migration has to apply, unchanged, everywhere. CI's test database is a bare
  `postgres:16` container with no `auth` schema, migrated with the same
  `prisma migrate deploy` production runs, so a cross-schema FK fails there outright. It
  also fails *locally*: Prisma's shadow database is a blank database that never inherits
  Supabase's schemas, which would break every subsequent `make migrate` too.

The consequence, stated here rather than discovered later: **Supabase Auth is the system of
record for users**, and Postgres will not stop a `websites` row from referencing a deleted
one. Deleting a user does not cascade to their websites. Nothing in this milestone deletes
users; whatever does, owns the cleanup.

---

## 7. Deploy policy

These are prohibitions, not preferences.

- **Merging to `main` with green CI triggers the deploy. That is the only path to
  production.** There is no manual promotion step and no alternate route.
- **Never run `fly deploy` from a laptop.** It bypasses CI, ships an image built from
  whatever happens to be in your working tree, and can leave production running against a
  migration revision that does not exist in the repo. The same applies to `vercel --prod`
  and to any other manual push of a build artifact.
- **Never deploy from a feature branch.** Deploys come from `main`.
- **Migrations run in a GitHub Actions job _before_ the Fly deploy.** A failed migration
  aborts the deploy. Application code therefore never lands ahead of the schema it needs.
- **Never apply a schema _or data_ change to production by hand**, through any channel —
  `psql`, the Supabase SQL editor, a one-off script, a Python shell on a Fly machine. The
  only way a change reaches the production database is a reviewed migration committed to
  this repo and applied by `prisma migrate deploy` in CI (§6). Read-only inspection —
  `SELECT`, reading logs and config — is fine.
- **Never deploy a rollback by re-running an old workflow.** Roll forward: revert the commit
  on `main`, let CI deploy the revert. Schema changes are not rolled back by redeploying an
  older image — an old image against a new schema is a second outage. A bad migration is
  corrected by a new migration.
- **Redeploying a previous image is break-glass, and it is a decision, not a convenience.**
  `fly deploy --image <previous>` restores availability while a revert is prepared; it is
  not the revert, and it is the _only_ acknowledged exception to the prohibition on
  deploying by hand. It is survivable solely because §6.3 requires every migration to be
  survivable by the release before it — it rolls back code and nothing else, and it leaves
  production running something that is not the head of `main`. Use it to stop the bleeding,
  then revert on `main` and let the pipeline deploy that.

The ordering is the point. Deploy is `migrate → deploy → smoke`, and every job is gated on
the one before it: a failed migration means no deploy, and a deploy that leaves the app
unhealthy fails the run rather than reporting success. One `fly deploy` rolls every process
in the image, so `app` and `worker` are not separately gated steps; they ship together
because they are one artifact.

`smoke` reads the `/health` body, and treats its two dependencies differently on purpose:
an unhealthy `db` fails the deploy, an unhealthy `redis` only warns. That is the same split
the endpoint itself encodes — the API cannot serve a single read without Postgres, and
serves every read without Redis, because it only ever enqueues. A deploy gate that
contradicted that would fail a release of a perfectly healthy API over a queue the API does
not need in order to serve.

This policy is restated in [`README.md`](./README.md#deploy-policy) so that it is visible to
anyone who reads only the README. **This section is the authoritative copy.** If the two ever
disagree, this one governs, and the README must be corrected to match. A deploy rule must
never exist only in the README.

---

## 8. Frontend conventions

### 8.1 App Router and the BFF

- **App Router only.** No `pages/` directory.
- **MDX is for prose pages, and there is one.** `@next/mdx` is wired up in
  `frontend/next.config.ts` — which is why `pageExtensions` lists `ts` and `tsx`
  explicitly: setting that key replaces the default list rather than extending it, and
  omitting them would unroute every other page in the app. `frontend/mdx-components.tsx`
  maps each element onto the palette's own tokens; it sits at the project root under that
  exact name because that is `@next/mdx`'s contract for the App Router, not a filing
  decision. No remark or rehype plugins, and no `@tailwindcss/typography` — a typography
  plugin brings its own colour opinions, including the `prose-invert` dark variant §8.5
  forbids. `app/docs/page.mdx` is the only MDX page. A second prose page is fine; a docs
  *site* (sidebar, search, version switcher) is a different ticket.
- Route handlers under `app/api/[...path]/` proxy to FastAPI. This is a
  backend-for-frontend: the browser calls same-origin Next.js routes, and Next.js calls Fly
  server-side.
- **There is no CORS configuration in this repo**, because the browser never issues a
  cross-origin request. If you find yourself adding CORS headers, you have accidentally
  called Fly from the client.

### 8.2 Auth and sessions

- The Supabase session lives in **cookies managed by `@supabase/ssr`**, never in
  `localStorage` or `sessionStorage`. `frontend/lib/supabase/client.ts`,
  `server.ts`, and `middleware.ts` are the only places that construct a Supabase
  client, and every one of them goes through `@supabase/ssr`'s cookie storage — there
  is no second, hand-rolled place that reads or writes the session.
- **Application code never reads the access token.** No call site pulls it out of a
  session to inspect, log, prop-drill through components, or copy into another store.
  The only reader of the session cookie is `@supabase/ssr` itself, plus the server-side
  proxy in `app/api/[...path]/`, which attaches it to the outbound FastAPI request —
  that is the one place a token is deliberately handled, and it happens entirely on the
  server.
- **Authorization decisions use `getClaims()` or `getUser()`, never `getSession()`.**
  `getSession()` returns whatever the cookie currently holds without revalidating it, so
  it must never gate access. `getClaims()` verifies the JWT (locally when the project
  uses asymmetric signing keys, otherwise via a call to the Auth server) and refreshes an
  about-to-expire session before returning; `frontend/lib/supabase/middleware.ts` calls
  it, not `getSession()`, for exactly this reason.
- **Route protection is enforced in `frontend/middleware.ts`, and only there** — never
  client-side. A client component may render differently for a signed-in vs. signed-out
  user (`frontend/lib/auth/use-user.ts`), but that is a display decision, not an
  authorization check; a page that must not be reached while signed out is gated by
  middleware, which runs on the server before any component renders.

**On `httpOnly`.** The session cookie is not marked `httpOnly`. That is a property of
`@supabase/ssr@0.12.4` — its default cookie options set `httpOnly: false` deliberately,
because `createBrowserClient` has to read the cookie itself to keep the session alive and
to drive `onAuthStateChange` in the browser — not a choice made in this repo, and not one
this repo can make differently without dropping `@supabase/ssr`. It is exactly why the
token itself is never what authorizes anything here: every read goes through a JWT-aware
Supabase call (`getClaims()`/`getUser()`), and every write is checked server-side, so
nothing in this system trusts the cookie's mere presence the way an `httpOnly`-only
threat model would.

### 8.3 Environment variables

- **`API_URL` is a server-only environment variable. It must never be prefixed
  `NEXT_PUBLIC_`.** Anything prefixed `NEXT_PUBLIC_` is compiled into the client bundle and
  is public forever.
- The Supabase **service-role key never appears in `frontend/`**, in any form, under any
  prefix. It is a backend credential.
- See §9 for the full secrets policy.

### 8.4 Components

| Directory | Contents |
| --- | --- |
| `components/ui/` | shadcn/ui primitives — generated, edited only when a primitive genuinely needs it |
| `components/magicui/` | Magic UI components |
| `components/crawls/` | App-specific composites built from the above |
| `components/landing/` | The landing page's own composites — the URL field and the account chip |
| `components/auth/` | Sign-in / sign-out affordances and the client-side identity hook |

Feature composites go in a directory named after the screen they belong to —
`components/crawls/` for the crawls table and detail page, `components/landing/` for `/`.
Do not put app-specific logic into `components/ui/`; those files should stay close to what
the generator produced so they can be regenerated. `components/magicui/` follows the same
rule, with one deliberate carve-out: retuning a Magic UI component for this light palette,
or adding the `asChild` escape hatch `components/ui/button.tsx` already has, is a change to
the primitive itself and belongs there — building a landing-page-shaped wrapper around it
does not.

**A screen's chrome is not shared by default.** `components/crawls/crawls-header.tsx` is
the app's header for signed-in screens; the landing page deliberately has none, and renders
only a `UserMenu` in the corner when there is a session. Importing a header in order to
hide most of it is how a page ends up with chrome nobody asked for.

**A feature's non-visual logic lives in `lib/`, not in its components.** `lib/crawls/` is
the first instance and sets the shape: the pure derivations behind the `/crawls` table
(what status a row is in — `row-status.ts`; how the list is ordered — `sort.ts`; how a
timestamp reads — `relative-time.ts`) and the hooks that are behaviour rather than markup
(the shared page clock, the status-change diff, whole-row activation). It is the same
separation §3.1 draws on the backend, and it exists for the same reason: a function that
turns four run statuses into five row labels is testable, greppable and reusable on its
own, and becomes none of those things once it is an `if` inside a `<td>`. The rule of
thumb is that anything in `lib/<feature>/` should be readable without knowing what the
screen looks like, and anything in `components/<feature>/` should be mostly markup.
`lib/landing/` is the second instance and holds exactly two things for the same reasons:
`site-url.ts`, the pure "is this an absolute http(s) URL" check, and `use-add-site.ts`, the
create-website-then-trigger-run-then-navigate sequence with its five endings. Note that
this is a *feature's own* logic: shared plumbing every feature uses stays in
`lib/api/`, `lib/query/`, `lib/auth/` and `lib/supabase/`, and a feature directory must
never grow a second copy of something those already own — a second fetcher, a second
query-key shape, or a second answer to "is this run still active" (§8.6). `use-add-site.ts`
is the worked example: it orchestrates, and every request it makes goes through
`lib/query/`'s existing `useCreateWebsite` and `useTriggerRun` so that the cache
invalidations stay defined once. What it *did* add to those two hooks is a
`toastOnError` option, defaulting to the existing behaviour — because the landing page
renders each failure under the field that caused it, and its two `409`s are navigations
rather than errors to report at all.

### 8.5 Light theme only

**This application is light-theme only.**

- Do not install or configure `next-themes`.
- Do not write `dark:` variants anywhere in the codebase.
- Do not add a theme toggle, a `prefers-color-scheme` media query, or a `.dark` class.

If a dark theme is ever wanted, it is a designed feature with its own ticket — not something
that accumulates one `dark:` class at a time.

**One generated primitive has to be re-edited every time it is regenerated.**
`frontend/components/ui/chart.tsx` ships from the shadcn registry with two-theme support: a
`THEMES = { light: "", dark: ".dark" }` map, a `theme` alternative to `color` in its config
type, and a `ChartStyle` that emits one CSS block per theme, the second prefixed `.dark`.
That half is deleted in this repo, and the file carries a `LOCAL EDIT` comment saying so.

It is worth knowing *why* the usual guardrail does not cover this one. `app/globals.css`'s
`@custom-variant dark (&:not(*))` neuters Tailwind's `dark:` **variant** — but what this
component emits is a raw `.dark [data-chart=…]` selector inside a `<style>` tag, which never
passes through Tailwind at all. The dead ruleset would ship on every chart and no static gate
would say a word. Re-running `npx shadcn@latest add chart` restores it; remove it again.

### 8.6 Data fetching

**The generated client is the contract.** `frontend/lib/api/openapi.json` is a checked-in
snapshot of `app.openapi()` — the same document FastAPI builds from the Pydantic models and
routes already described in §3 — and `frontend/lib/api/schema.d.ts` is generated from that
snapshot by `openapi-typescript` (`npm run gen:api`, or `make openapi` for both halves at
once). No frontend code hand-writes a request or response shape: `frontend/lib/api/fetcher.ts`
exports a small typed client (`api.get`/`api.post`/`api.put`/`api.delete`) generic over the
generated `paths` type, and every feature-level helper (`frontend/lib/api/websites.ts`,
`runs.ts`, `schedules.ts`, `health.ts`) calls through it and re-exports readable aliases of
`components["schemas"][...]`. Calling a path that does not exist, or getting a parameter or a
body wrong, is a `tsc` error, not a runtime one.

One maintenance note on that client, because it fails quietly rather than loudly. It resolves
an operation's response type by looking up the 2xx status the operation declares, against a
fixed `SuccessStatus = 200 | 201 | 202 | 204` union in `fetcher.ts`. A backend endpoint
returning a 2xx that is *not* in that union resolves to `never` — and because `never` is
assignable to everything, a helper declaring its own return type still compiles while the
client's inferred type says the call returns nothing. `202` had to be added when PER-160's
`POST /websites/{id}/runs` landed, for exactly that reason. Any future 2xx needs the same
one-line entry.

**The drift check has two halves, enforced separately.** `scripts/export-openapi.sh --check`
(run in `ci-backend.yml`'s `lint` job, and by `make lint`) re-exports the live schema from
`app.openapi()` and diffs it against the committed `openapi.json` — this is what catches a
Pydantic model changing without the snapshot being regenerated. `ci-frontend.yml` separately
regenerates `schema.d.ts` from the committed `openapi.json` and diffs *that* — this is what
catches a hand-edit of the generated TypeScript, and it needs no Python and no running
backend, because it only ever reads the JSON already in the repo. Both checks fail with the
`make` target that fixes them (`make openapi`) named in the error.

**The query key factory.** `frontend/lib/query/query-keys.ts` is the only place a React
Query cache key is constructed — `queryKeys.websites.all`, `.list(include?)`,
`.detail(id)`, the mirroring `queryKeys.runs.all`, `.list(websiteId, options?)`,
`.detail(id)`, `queryKeys.schedules.detail(websiteId)`, and
`queryKeys.stats.detail(websiteId, window)` — so that invalidating "every website" or "this
one website" (or "every run" or "this one run", or "this one website's schedule") is a call
to a function here rather than an array literal a caller has to get byte-for-byte right at
every call site. `schedules` has no `.all`/`.list` of its own: a schedule is 1:1 with a
website and has no independent id anywhere in the API surface, so `websiteId` alone is both
the scope and the whole key, unlike `runs`, which is genuinely a collection per website.

`stats` (`GET /websites/{id}/stats`, the Trends tab) is a separate root rather than a
`runs.*` key, even though `lib/api/runs.ts` owns its `getStats` helper and it aggregates the
very same rows. `runs.forWebsite(id)` exists to be invalidated and is a prefix of
`["runs", "list", websiteId]`; nesting stats under `runs` would either fall outside that
prefix, making the nesting decorative, or inside it, so that every "this website's history
changed" invalidation also dropped an aggregate the Trends tab may be rendering. Its
`window` is part of the key for the reason `include` is part of `websites.list`: `?window=7d`
and `?window=90d` are different responses with different `bucket` fields and different
series lengths, and one key for both would render 7d's hourly buckets under day labels until
the refetch landed.

`runs` carries two further keys, both added for the detail page. `.infinite(websiteId,
filters?)` is the same list read through `useInfiniteQuery` rather than `useQuery`
(`frontend/lib/query/use-runs-infinite.ts`, the Runs tab's "Load more"); it is a separate key
from `.list` because the two cache genuinely different shapes — an infinite query stores
`{ pages, pageParams }` where a plain one stores a bare `Page[RunListItemResponse]` — and its
`filters` are `RunListOptions` **minus** `cursor`, since in an infinite query the cursor is
the page parameter rather than part of the key. `.forWebsite(websiteId)` is a prefix of both
and is never fetched with: it exists so `useTriggerRun` can invalidate every cached page and
filter variant of one website's history in a single call. That is deliberately narrower than
`runs.all`, which would also drop every `runs.detail` entry — including the `llms_txt` the
Output tab may be displaying for a run that certainly did not change.

**Polling.** A query polls only while something in its data is still in progress, at
`ACTIVE_POLL_INTERVAL_MS` (3 seconds), and stops the moment it isn't —
`frontend/lib/query/polling.ts`'s `pollWhileActive` builds a `refetchInterval` callback from
a predicate over that query's own data, so "is a run still running" is decided in exactly
one place regardless of which query asks. `frontend/lib/api/run-status.ts`'s
`isActiveRunStatus` — an exhaustive `Record` over the `runs.status` enum — is that one
place, and three call sites currently build a `pollWhileActive` predicate on top of it, one
per response shape that carries a status: `anyWebsiteHasActiveRun` for the websites list
(`useWebsites`, `GET /websites?include=latest_run`), `anyRunActive` for a website's run
history (`useRuns`, `GET /websites/{id}/runs`), and `runIsActive` for a single run's own
detail (`useRun`, `GET /runs/{id}`). A fourth has since landed and follows the same rule
rather than bending it: `anyRunActiveInPages` (`frontend/lib/query/use-runs-infinite.ts`) is
`anyRunActive` folded over an `InfiniteData`'s pages, so the accumulating Runs tab shares the
one definition of "still running" with everything else. It lives in `lib/query/` rather than
beside its three siblings in `lib/api/run-status.ts` because `InfiniteData` is a React Query
type and the `lib/api/` layer describes the backend's shapes without knowing what this app
caches them in. A fifth call site means a fifth thin fold over `isActiveRunStatus`, never a
fresh string comparison.

Supporting that fourth fold, `pollWhileActive` takes two type parameters —
`pollWhileActive<TQueryFnData, TData = TQueryFnData>`. They are the same type for an ordinary
`useQuery`, which is why the default exists and why no plain call site passes either. They
come apart only for an infinite query, which fetches one page but caches all of them, and
React Query types `refetchInterval`'s argument over both; without the split, an infinite
query could not use this helper at all and would have had to inline its own interval. Polling also pauses on a hidden tab
(`refetchIntervalInBackground: false`, set as a `QueryClient` default in `app/providers.tsx`
rather than trusted to every `useQuery` call) — a run that takes ten minutes should not poll
a tab nobody is looking at roughly 200 times. `useWebsite` (`GET /websites/{id}`, a single
website with no run or schedule information on that endpoint) deliberately does not poll at
all; a detail screen composes it with `useRuns`/`useRun` instead, which is where the run
data — and therefore the polling — actually lives. Inventing a poll on `useWebsite` itself
would be worse than not polling, because it would look like a feature and do nothing.
`useSchedule` (`GET /websites/{id}/schedule`) does not poll either, for a related but
distinct reason: a schedule has no in-progress state at all — there is nothing analogous to
`pending`/`processing` for `pollWhileActive` to key off — so it changes only when a person
submits `PUT /websites/{id}/schedule`, and `usePutSchedule` already invalidates
`queryKeys.schedules.detail(websiteId)` (plus `queryKeys.websites.all`, since `GET
/websites?include=latest_run` folds a `ScheduleSummary` into every row) the moment that
mutation succeeds. Polling a resource that only a mutation this app already controls can
change would just be a slower, wasteful copy of the invalidation that already fires
immediately.

---

## 9. Secrets hygiene

**This repository is public.** Everything committed to it is visible to anyone, forever,
including in the history of branches and pull requests that were never merged. The rules
below are prohibitions, not preferences, and they apply to every commit by every author,
human or agent.

### 9.1 Never commit a secret

**Never commit a real secret value to this repository, in any commit, at any point in
history.** That includes, and is not limited to:

- Private keys and certificates (`*.pem`, `*.key`, SSH keys, signing keys)
- API keys and tokens of any kind (Supabase anon and service-role keys, OpenAI/Anthropic
  keys, Fly tokens, Vercel tokens, GitHub PATs)
- Connection strings that contain credentials (`postgres://user:password@host/db`)
- Session cookies, JWTs captured from a real session, webhook signing secrets
- Anything a service would let you rotate — if it can be rotated, it is a secret

This applies to source files, tests, fixtures, snapshots, notebooks, seed data, screenshots,
and documentation. A "temporary" or "throwaway" key is still a secret.

### 9.2 `.env.example` carries placeholders only

- `.env.example` is committed, and contains **placeholder values only** — never a real one.
  Use obvious non-values: `SUPABASE_SERVICE_ROLE_KEY=<your-service-role-key>`.
- `.env`, `.env.local`, and every other `.env.*` file are gitignored and stay that way. The
  committed `.gitignore` covers them; verify it still does before adding a new env file
  convention.
- **Never remove or weaken the `.env` entries in `.gitignore`** to make a local workflow more
  convenient.

### 9.3 Secrets are supplied at runtime, by the platform

Secrets reach running code through platform secret stores, and by no other route:

| Where it runs | Secret store |
| --- | --- |
| Fly.io (API and worker) | `fly secrets set` |
| Vercel (frontend) | Vercel project environment variables |
| GitHub Actions (CI, migrations, deploy) | GitHub Actions secrets |

**Never paste a secret value into a pull request description, an issue, a code review
comment, a commit message, a log line, or CI output.** Those surfaces are public and are
indexed.

### 9.4 Never echo or log a secret

**Never print a secret value from application code, a script, or a CI step.** No
`echo $DATABASE_URL`, no `print(settings.supabase_service_role_key)`, no logging a request
header that carries an `Authorization` value, no dumping the whole settings object on
startup.

- Log the **name** of a missing variable, never its value.
- Redact `Authorization` and `Cookie` headers before logging a request.
- Do not enable shell tracing (`set -x`) in a CI step that has secrets in its environment.

### 9.5 If a secret is ever committed, rotate it

Git history is permanent, and this repository is public. Assume that anything pushed has
already been scraped.

**Rotating the credential is mandatory. Removing it in a later commit is NOT sufficient**,
and neither is force-pushing, rewriting history, or deleting the branch. The order is:

1. **Rotate the credential at its source, immediately.** Revoke the old value.
2. Update the platform secret store with the new value.
3. Remove the value from the working tree and push.
4. Say so in the PR or issue — without repeating the value.

Rewriting history is optional cleanup after rotation. It is never a substitute for it.

---

## 10. Naming conventions

### 10.1 Python

- Files and functions: `snake_case`. Classes: `PascalCase`.
- Line length: **100**.
- Readers and writers are named for their feature: `websites_reader.py`, `websites_writer.py`.
- Service classes are `{Feature}Service` (`WebsiteService`, `RunService`).
- Pydantic DTOs are `{Verb}{Noun}Request` and `{Noun}Response`
  (`CreateWebsiteRequest`, `WebsiteResponse`).

### 10.2 TypeScript

- Files: `kebab-case` (`website-card.tsx`, `use-runs.ts`).
- Components: `PascalCase` (`WebsiteCard`).
- Functions and variables: `camelCase`.
- Types and interfaces: `PascalCase`.

### 10.3 API routes

Plural nouns, nested under their parent, **no version prefix for now**:

```
GET    /websites
POST   /websites
GET    /websites/{id}
DELETE /websites/{id}
GET    /websites/{id}/runs
POST   /websites/{id}/runs
GET    /runs/{id}
GET    /websites/{id}/stats
GET    /websites/{id}/schedule
PUT    /websites/{id}/schedule
```

- Path parameters are `{id}`, not `{website_id}`, when the noun is already in the path.
- No verbs in paths. `POST /websites/{id}/runs` starts a run; there is no `/start-crawl`.
- `schedule` is the one exception to "plural nouns": it is a singleton sub-resource, at most
  one per website and enforced as such by `schedules`' `UNIQUE (website_id)` index, so there
  is never a collection of them to name in the plural.
- No `/v1` prefix. When versioning is genuinely needed, it gets a ticket and a migration
  plan — it is not added preemptively.

### 10.4 Database

- Tables: plural `snake_case` (`websites`, `runs`).
- Columns: `snake_case`. Primary keys are `id` (UUID). Foreign keys are `{singular}_id`
  (`website_id`, `user_id`).
- Timestamps: `created_at`, `updated_at`, `timestamptz`, always UTC.

---

## 11. Out of scope

Deliberately not decided here, and not to be decided by accident in an implementation PR:

- **The crawler pipeline design.** That milestone has not been designed. Until it is,
  everything sits behind `generate_llms_txt(pages) -> str` (§3.4). Two pieces have landed,
  both pure and both called by nothing (§3.4): `internals/extract.py`, which turns one
  page's HTML into text, and `internals/url_ranking.py`, which decides which discovered URLs
  are worth fetching at all. Together they still do not constitute a design for the rest —
  how *fetched* pages are ranked, what a generated artifact selects and summarizes, and
  whether a model is involved at all are all still open.
- **Multi-tenancy.** This project has per-user ownership and nothing more (§4).
- **Dark mode** (§8.5) and **API versioning** (§10.3).
- **Rate limiting, quotas, and billing.**
- **Cleaning up orphaned Storage objects.** `CrawlService.execute_run` uploads a run's
  payload to Storage before `RunService.record_success` writes the row that names it (§5.1),
  and deleting a website cascades its `runs` rows but never touches Storage (§6.4) — both are
  deliberate, and both leave objects in the `crawl-payloads` bucket that nothing in this
  codebase ever removes.
  Two candidate fixes, neither built here: a `DELETE /websites/{id}` hook that removes the
  website's whole `{website_id}/` prefix at delete time, or a periodic sweep that lists
  objects with no matching `runs.storage_path`. Whichever a future ticket picks, it needs its
  own design for how it authenticates to Storage and how it is triggered — this milestone
  only establishes the layout (`{website_id}/{run_id}.jsonl.gz`) that makes either one cheap
  to write later.

If you need one of these to finish a ticket, that is a signal to open a ticket, not to
invent an answer inline.
