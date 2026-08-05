# CLAUDE.md — llms-text

Rules for working in this repo. **Read [`ARCHITECTURE.md`](./ARCHITECTURE.md) first** — it is
the engineering contract, and it wins over any ticket that contradicts it. This file is not a
summary of that document; it is the short list of rules that are most expensive to get wrong.

**Stack:** Next.js App Router on Vercel · FastAPI + ARQ worker on Fly.io (one image, two
processes) · Supabase Postgres / Auth / Storage · Prisma for schema and migrations only ·
asyncpg at runtime.

---

## Non-negotiables

**1. Never commit a secret.** This repository is **public** and its history is permanent.
Never commit a private key, API key, service-role key, token, or a connection string
containing credentials — in any commit, on any branch, ever. `.env.example` holds
placeholders only; `.env` and `.env.local` are gitignored and stay that way. Never echo or
log a secret value in code, a script, or a CI step (no `echo $DATABASE_URL`, no dumping the
settings object). Never paste one into a PR description, an issue, or a review comment.
**If a secret is ever committed, rotating it is mandatory — deleting it in a later commit is
not sufficient.** This rule is first because it is the only one on this list that cannot be
undone. Full policy: [ARCHITECTURE.md §9](./ARCHITECTURE.md#9-secrets-hygiene).

**2. Never run a migration by hand, and never run `prisma db push`.** `db/schema.prisma` is
the only source of truth. Author migrations with `prisma migrate dev --create-only`, read the
generated SQL, and commit it alongside the schema change. CI applies it with
`prisma migrate deploy`. Never hand-apply SQL to production — not through `psql`, not through
the Supabase SQL editor. Never edit a migration that has already been applied; write a new
one.

**3. Never deploy from a laptop.** Merging to `main` with green CI is the only path to
production. No `fly deploy`, no `vercel --prod` — a local deploy bypasses CI and can leave
production on a migration revision that no longer exists in the repo.

**4. Reads are unscoped; writes are ownership-checked.** This project is **not
multi-tenant**. Read endpoints require a valid JWT and **do not** filter by `user_id` — every
signed-in user can read every website and every run. Do not "helpfully" add
`WHERE user_id = $1` to a read query. Writes require a valid JWT **and** ownership, checked
by `require_owner(resource, user_id)` in the service method as soon as the resource is
fetched — before any mutation, before any transaction, before any external call. Non-owners
get `403`. There is no `tenant_id` in this codebase and no ticket adds one.

**5. Transactions live in the service layer; writers do not commit.** A service opens an
async `transaction()` that commits on success and rolls back on any exception. A writer
executes its statement and returns — it never commits, never rolls back, never opens a
transaction. **External calls happen outside transactions:** do the Storage upload or HTTP
fetch first, then open a short transaction to record the result. Never hold a database
transaction open across a network call.

**6. Routers are thin.** A route handler parses input, calls one service method, and returns
the response. No business logic, no SQL, no `pool.execute` in a router. All `SELECT`s live in
the feature's reader; all `INSERT`/`UPDATE`/`DELETE` in its writer.

**7. Light theme only.** Do not install `next-themes`. Do not write `dark:` variants
anywhere. Do not add a theme toggle or a `prefers-color-scheme` query. Dark mode is a
designed feature with its own ticket, not something that accumulates one class at a time.

**8. The browser never calls Fly, and the frontend never touches the database.** All frontend
requests go through the Next.js route handlers under `app/api/[...path]/`, which proxy to
FastAPI server-side. There is **no CORS configuration in this repo** — if you are adding CORS
headers, you have accidentally called the API from the client. `API_URL` is server-only and
must never be prefixed `NEXT_PUBLIC_`.

**9. Real crawling logic is out of scope.** That milestone has not been designed. Everything
downstream of a fetched page stays behind one seam:

```python
def generate_llms_txt(pages: list[Page]) -> str:
    ...
```

Build against that signature. Do not scatter crawling, parsing, or LLM-calling logic through
the services in anticipation of a design that does not exist yet.

---

## Where things go

```
frontend/                       Next.js App Router → Vercel
backend/app/api/routers/        thin HTTP handlers
backend/app/features/<name>/    schemas.py, service.py, internals/{<name>_reader,<name>_writer}.py
backend/app/worker/             ARQ job functions — thin, they call services
db/schema.prisma                the schema, and the only source of truth for it
db/migrations/                  reviewed, committed SQL
```

Import direction is one-way: `api` → `features` → `core`. No feature imports another
feature's `internals/` — it calls that feature's service.

**Naming:** Python `snake_case` files and functions, `PascalCase` classes, line length 100.
TypeScript `kebab-case` files, `PascalCase` components, `camelCase` functions. API routes are
plural nouns with no version prefix: `/websites`, `/websites/{id}/runs`, `/runs/{id}`.

---

## Commands

```bash
make dev        # run frontend, API, and worker locally
make migrate    # create a migration from schema.prisma (review the SQL, then commit it)
make test       # backend and frontend test suites
make lint       # ruff for backend, eslint + prettier for frontend
```

**The `Makefile` does not exist yet** — it lands with the local dev environment ticket, and
these are the names it will use. Until then, run the underlying tools directly. When you add
a target to the `Makefile`, add it to this list in the same PR.

There is no CI workflow yet either; it lands with the CI ticket. Once it exists, it runs the
same commands, path-filtered by stack.

---

## Checklist for a new feature

- [ ] Feature module is `backend/app/features/<name>/` with `schemas.py`, `service.py`, and `internals/`
- [ ] Every `SELECT` is in the reader; every write is in the writer; the writer does not commit
- [ ] Router is thin — parse, call one service method, return
- [ ] Read endpoints do **not** filter by `user_id`
- [ ] Every write path calls `require_owner(...)` right after fetching the resource, with nothing but the fetch above it
- [ ] Transactions are opened in the service, and no network call happens inside one
- [ ] Schema change and its generated migration are committed together, with the SQL read
- [ ] No new `NEXT_PUBLIC_` variable holds anything sensitive
- [ ] No secret, key, token, or credentialed connection string appears in the diff
- [ ] No `dark:` variant, no `next-themes`
- [ ] `ARCHITECTURE.md` still describes what the code does — if not, update it in this PR
