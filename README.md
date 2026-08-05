# llms-text

Generate and maintain [`llms.txt`](https://llmstxt.org) files for websites. A signed-in user
registers a **website** by domain; the backend crawls it as a **run**; the run produces an
`llms.txt` artifact that describes the site in the form large language models can consume.
Runs are kept, so a website accumulates a history and you can see what changed between
crawls.

The application is deliberately small and flat. It is **not multi-tenant**: there is one pool
of websites and runs, every signed-in user can read all of it, and only the owner of a
resource can change it. The frontend is a Next.js App Router app on Vercel that talks to a
FastAPI service on Fly.io; a background ARQ worker runs from the same image and does the
crawling. Supabase provides Postgres, Auth, and Storage. Prisma manages the schema and its
migrations, and nothing else — at runtime the backend talks to Postgres over asyncpg.

> **Status: early.** The architecture documents landed before the code did, on purpose. See
> [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the engineering contract and
> [`CLAUDE.md`](./CLAUDE.md) for the short list of rules that are expensive to get wrong.
> Sections below marked _forthcoming_ are filled in by the ticket named beside them.

---

## Architecture

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

```
llms-text/
├── frontend/            Next.js → Vercel (Vercel root dir = frontend/)
├── backend/             FastAPI + ARQ worker → Fly.io (one image, two processes)
├── db/                  schema.prisma + migrations/ (Prisma CLI lives here)
└── .github/workflows/   path-filtered CI + deploy
```

Full detail, including the authorization contract and the transaction rules, is in
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Node | 20+ | `frontend/`, and the Prisma CLI in `db/` |
| Python | 3.12+ | `backend/` API and worker |
| Docker | current | local Postgres and Redis |
| Supabase CLI | current | local Supabase stack, auth, storage |
| Fly CLI (`flyctl`) | current | inspecting deployments and setting secrets |

---

## Run locally

_Forthcoming — filled in by the local dev environment ticket._

That ticket lands the `Makefile`. The command names are already fixed:

```bash
make dev        # run frontend, API, and worker locally
make migrate    # create a migration from schema.prisma
make test       # backend and frontend test suites
make lint       # ruff for backend, eslint + prettier for frontend
```

---

## Environment variables

_Forthcoming — filled in by the settings ticket, which lands `.env.example` and the settings
module that validates configuration at startup._

Two rules apply now and are not negotiable later:

- **`API_URL` is server-only.** It must never be prefixed `NEXT_PUBLIC_`. Anything with that
  prefix is compiled into the client bundle and is public forever.
- **No real secret value is ever committed to this repository.** `.env.example` carries
  placeholders only; `.env` and `.env.local` are gitignored. Secrets are supplied at runtime
  through Fly secrets, Vercel environment variables, and GitHub Actions secrets. The full
  policy — including what to do if a secret is ever committed — is
  [ARCHITECTURE.md §9](./ARCHITECTURE.md#9-secrets-hygiene). This repository is public; read
  it before you add an environment variable.

---

## Deploy

_Forthcoming — filled in by the deploy pipeline ticket, which lands the GitHub Actions
workflows._

The mechanism is forthcoming. **The policy below is not** — it is written now, in full,
because it constrains how that pipeline gets built.

### Deploy Policy

These are prohibitions, not preferences. They exist because the failure they prevent — a
production database on a schema that no commit in this repo describes — is slow to notice and
expensive to unwind. If you want an exception, talk to Justin first; do not take one
unilaterally.

This restates [`ARCHITECTURE.md` §7](./ARCHITECTURE.md#7-deploy-policy) — plus the migration
prohibitions from [§6.3](./ARCHITECTURE.md#63-prohibitions), which reach production through
the deploy and so belong in front of anyone reading this policy. `ARCHITECTURE.md` is the
authoritative copy. If the two disagree, it governs. Never add a rule here without adding it
there.

- **Merging to `main` with green CI triggers the deploy. That is the only path to
  production.** There is no manual promotion step and no alternate route.
- **Never run `fly deploy` from a laptop.** It bypasses CI and ships an image built from
  whatever happens to be in your working tree — which can leave production running against a
  migration revision that does not exist in the repo. The same applies to `vercel --prod` and
  to any other manual push of a build artifact.
- **Never deploy from a feature branch.** Deploys come from `main`.
- **Migrations run in a GitHub Actions job _before_ the Fly deploy.** The order is
  `migrate → deploy web → deploy worker`, and every step is gated on the one before it. A
  failed migration aborts the deploy, so application code never lands ahead of the schema it
  needs.
- **Never apply a schema or data change to production by hand**, through any channel —
  `psql`, the Supabase SQL editor, a one-off script, a Python shell on a Fly machine. The
  only way a change reaches the production database is a reviewed migration committed to this
  repo and applied by `prisma migrate deploy` in CI. Read-only inspection (`SELECT`, reading
  logs and config) is fine.
- **Never run `prisma db push`.** Anywhere. It mutates a database without producing a
  migration, which desynchronizes the schema from its recorded history.
- **Roll forward, not back.** To undo a bad deploy, revert the commit on `main` and let CI
  deploy the revert. Do not re-run an old workflow to redeploy an old image — the schema has
  already moved, and an old image against a new schema is a second outage. A bad migration is
  corrected by a new migration.

---

## Documentation

| Document | What it is |
| --- | --- |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | The engineering contract — layout, layering, authorization, transactions, migration and deploy policy, secrets hygiene, naming |
| [`CLAUDE.md`](./CLAUDE.md) | Rules for agents and humans working in this repo — the short list, plus a per-feature checklist |
| `README.md` | This file — what this is, how to run it, and the deploy policy |

Every ticket in this project references `ARCHITECTURE.md`. If a ticket contradicts it, the
document wins and the ticket should be corrected.
