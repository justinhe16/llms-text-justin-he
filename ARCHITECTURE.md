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
                       │  │  (web)     │ (worker)   │ │
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
| `backend/` | FastAPI + ARQ | Fly.io | One image, two processes (`web`, `worker`) |
| `db/` | Prisma CLI | — | Schema and migration tooling only; never runs in production |
| Postgres / Auth / Storage | Supabase | — | Backend talks to Postgres over **asyncpg** |
| Job queue | Redis | — | Required by ARQ; provisioning is handled by the infra ticket |

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
llms-text/
├── frontend/            Next.js → Vercel (Vercel root dir = frontend/)
├── backend/             FastAPI + ARQ worker → Fly.io (one image, two processes)
├── db/                  schema.prisma + migrations/ (Prisma CLI lives here)
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
  no Nx).
- **CI is path-filtered.** A frontend-only change must not run the backend test suite, and
  vice versa. Workflows live in `.github/workflows/` and land in the CI ticket.

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
    └── websites_writer.py  All INSERT/UPDATE/DELETE
```

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

**Import direction is one-way:** `api` → `features` → `core`. Nothing in `core` may import
from `api` or `features`, and no feature may import another feature's `internals/`. Import
absolutely — `from app.core.settings import settings`, never `from ..core import settings`.

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

### 3.4 The crawler seam

Real crawling and extraction logic is **out of scope for this milestone** and has not been
designed yet. Until it is, everything downstream of a fetched page sits behind one function:

```python
def generate_llms_txt(pages: list[Page]) -> str:
    ...
```

Build against that signature. Do not scatter crawling, parsing, or LLM-calling logic
through the services in anticipation of a design that does not exist yet, and do not widen
the signature without a ticket that redesigns this seam.

---

## 4. The authorization contract — public read, owner write

**This project is not multi-tenant.** There is one flat pool of websites and runs, and every
signed-in user can read all of it. There is no `tenant_id`, no tenant-scoped reader, no
`X-Tenant-ID` header, and no tenant validation helper anywhere in this codebase. If you are
porting a pattern from a multi-tenant project, leave that machinery behind.

The contract has exactly two halves:

**Reads — authenticated, unscoped.**
Read endpoints require a valid Supabase JWT, and **do not** filter by `user_id`. Any
signed-in user sees every website and every run, including `llms_txt` content.

**Writes — authenticated and owned.**
`POST`, `PATCH`, `PUT`, and `DELETE` require a valid JWT **and** ownership of the resource.
A non-owner gets `403`, not `404`.

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

Ownership is checked with a single shared helper:

```python
require_owner(resource, user_id)  # raises 403 if resource.user_id != user_id
```

It is called **as soon as the resource is in hand, and before any other work** — before any
mutation, before any transaction is opened, and before any external call. Fetching the
resource is the only thing allowed to precede it, because you cannot check an owner without
the row:

```python
async def delete_website(self, website_id: UUID, user_id: UUID) -> None:
    website = await self._reader.get_by_id(website_id)   # 404 if missing
    require_owner(website, user_id)                      # 403 if not owner
    async with self.transaction() as tx:
        await self._writer.delete(tx, website_id)
```

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
    async with self.transaction() as tx:
        await self._writer.update_status(tx, run_id, status="succeeded")
        await self._writer.set_output(tx, run_id, storage_url, llms_txt)
```

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
async with self.transaction() as tx:
    await self._writer.set_output(tx, run_id, storage_url, llms_txt)

# WRONG — a slow upload holds a Postgres transaction open the whole time
async with self.transaction() as tx:
    storage_url = await self._storage.upload(key, content)
    await self._writer.set_output(tx, run_id, storage_url, llms_txt)
```

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

The ordering is the point. Deploy is `migrate → deploy web → deploy worker`, and every step
is gated on the one before it.

This policy is restated in [`README.md`](./README.md#deploy-policy) so that it is visible to
anyone who reads only the README. **This section is the authoritative copy.** If the two ever
disagree, this one governs, and the README must be corrected to match. A deploy rule must
never exist only in the README.

---

## 8. Frontend conventions

### 8.1 App Router and the BFF

- **App Router only.** No `pages/` directory.
- Route handlers under `app/api/[...path]/` proxy to FastAPI. This is a
  backend-for-frontend: the browser calls same-origin Next.js routes, and Next.js calls Fly
  server-side.
- **There is no CORS configuration in this repo**, because the browser never issues a
  cross-origin request. If you find yourself adding CORS headers, you have accidentally
  called Fly from the client.

### 8.2 Auth and sessions

- The Supabase session lives in **httpOnly cookies**, managed by `@supabase/ssr`.
- **The access token is never exposed to client JavaScript.** Do not read it in a client
  component, do not put it in `localStorage`, do not pass it as a prop.
- Server-side code attaches the token to the outbound FastAPI request. That is the only
  place it is read.

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

Feature composites go in `components/crawls/`. Do not put app-specific logic into
`components/ui/`; those files should stay close to what the generator produced so they can
be regenerated.

### 8.5 Light theme only

**This application is light-theme only.**

- Do not install or configure `next-themes`.
- Do not write `dark:` variants anywhere in the codebase.
- Do not add a theme toggle, a `prefers-color-scheme` media query, or a `.dark` class.

If a dark theme is ever wanted, it is a designed feature with its own ticket — not something
that accumulates one `dark:` class at a time.

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
```

- Path parameters are `{id}`, not `{website_id}`, when the noun is already in the path.
- No verbs in paths. `POST /websites/{id}/runs` starts a run; there is no `/start-crawl`.
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
  everything sits behind `generate_llms_txt(pages) -> str` (§3.4).
- **Multi-tenancy.** This project has per-user ownership and nothing more (§4).
- **Dark mode** (§8.5) and **API versioning** (§10.3).
- **Rate limiting, quotas, and billing.**

If you need one of these to finish a ticket, that is a signal to open a ticket, not to
invent an answer inline.
