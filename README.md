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
llms-text-justin-he/
├── frontend/            Next.js → Vercel (Vercel root dir = frontend/)
├── backend/             FastAPI + ARQ worker → Fly.io (one image, two processes)
├── db/                  schema.prisma + migrations/ (Prisma CLI lives here)
├── supabase/            local Supabase stack config (config.toml, seed.sql)
├── scripts/             shell helpers used by the Makefile (dev.sh, local-env.sh)
├── docker-compose.yml   local Redis only — Supabase is managed by its own CLI
├── Makefile             local dev commands — see "Run locally" below
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
| Supabase CLI | **2.111.0, pinned** | local Supabase stack, auth, storage |
| Fly CLI (`flyctl`) | current | inspecting deployments and setting secrets |

The Supabase CLI is pinned, not just "current": `supabase/config.toml` was written and
verified against 2.111.0, and config keys and defaults have moved between CLI releases.
Install that exact version — Homebrew (`brew install supabase/tap/supabase`),
`npm install -g supabase@2.111.0`, or any method from the CLI's own install docs — and
confirm with `supabase --version` before running `supabase start` or `make dev`.

---

## Run locally

### First run

1. Install the prerequisites above. In particular, pin the **Supabase CLI to 2.111.0** —
   see the note under Prerequisites.
2. `make setup` — creates `backend/.venv`, installs backend dependencies, installs the
   Prisma CLI in `db/`, and installs `frontend/` dependencies. Run this once per checkout,
   and again after pulling a dependency change.
3. `make dev` — starts the local Supabase stack (Postgres, Auth, Storage) and Redis in
   Docker, then the FastAPI API with autoreload and the Next.js dev server, each with its
   own log prefix. It prints the table of URLs below once everything is up. The ARQ worker
   is skipped with a note until its ticket lands — that's expected, not a failure.
4. `Ctrl-C` stops the API and frontend (and the worker, once it exists). Supabase and
   Redis keep running in Docker after that — local Postgres data and the seeded test user
   persist across `make dev` sessions. `make down` stops those containers too.

Every target below is documented with `make help`.

```bash
make help          # list every target, with its one-line purpose
make setup         # create backend/.venv, install backend + db deps (frontend if present)
make dev           # run Supabase, Redis, the API, worker, and frontend locally
make migrate       # create a migration from schema.prisma (review the SQL, then commit it)
make migrate-apply # apply pending migrations to the local database
make test          # backend and frontend test suites
make lint          # ruff + mypy for backend, eslint + tsc for frontend
make down          # stop the Supabase and Redis containers
make reset         # recreate the local database, reseed it, replay Prisma migrations
```

### URLs

Printed by `make dev` once the stack is up:

| Service | URL |
| --- | --- |
| App (Next.js) | http://localhost:3000 |
| API (FastAPI) | http://localhost:8000 |
| API docs (Swagger UI) | http://localhost:8000/docs |
| Supabase Studio | http://localhost:54323 |

### Local test user

A fixed test user is seeded automatically the first time the local database initializes,
and every time you run `make reset` (`supabase/seed.sql`):

| Field | Value |
| --- | --- |
| Email | `dev@llms-text.test` |
| Password | `devpassword123` |

This password is not a secret worth protecting — it unlocks a Postgres container running
on your own machine, nothing reachable from outside it. **Local sign-in is email/password
only.** GitHub OAuth is configured and verified against the production Supabase project;
it is deliberately left disabled in `supabase/config.toml` for local development.

### Troubleshooting

**A port this stack needs is already in use.** Local Supabase uses 54321 (API/Auth/
Storage gateway), 54322 (Postgres), 54323 (Studio), and 54324 (local email testing);
Redis uses 6379; the API uses 8000; the frontend, once it exists, uses 3000. Find what's
holding a port with `lsof -nP -iTCP:<port> -sTCP:LISTEN`, and stop it — or see "stale
Supabase containers" below if the culprit is a leftover container of your own. If whatever
holds port 8000 isn't yours to stop, move the API for the session with
`API_PORT=8001 make dev` (`API_HOST` overrides the same way). The Supabase ports are pinned
in `supabase/config.toml` and should be changed there rather than on the command line.

**Docker isn't running.** `make dev` and friends check for a live Docker daemon (not just
the `docker` binary) before doing anything else, and fail with a message telling you to
start Docker Desktop, rather than an opaque connection error partway through.

**Stale Supabase containers from a previous run.** `make down` stops both the Supabase
and Redis containers cleanly and is the normal way to shut the stack down between
sessions. If a container is stuck anyway, `docker ps -a` to find it and `docker rm -f
<name>`, then `make dev` again — `supabase start` recreates whatever it needs.

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

## Infrastructure

One production environment; no staging. Everything below is provisioned.

| Service | Resource | Purpose |
| --- | --- | --- |
| Supabase | `iulfhmykutevtrgcaiec` · us-west-1 | Postgres, GitHub OAuth, private `crawl-payloads` bucket |
| Upstash | `llms-txt-prod` · us-west-1 | Redis backing the ARQ job queue |
| Fly.io | `llms-text-justin-he` · org `personal` | FastAPI API + ARQ worker |
| Vercel | `llms-text-justin-he` · scope `justinhe16s-projects` | Next.js frontend, root directory `frontend/` |

### Where each credential lives

No credential is stored in this repo. Each one lives in exactly one place:

| Name | Stored in | Where it comes from |
| --- | --- | --- |
| `DATABASE_URL` | Fly secrets | Supabase → Settings → Database → **Session pooler** |
| `REDIS_URL` | Fly secrets | `upstash redis get --db-id <id>` |
| `SUPABASE_URL` | Fly secrets, Vercel env | Supabase → Settings → API → Project URL |
| `SUPABASE_SECRET_KEY` | Fly secrets | Supabase → Settings → API → secret key |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Vercel env | Supabase → Settings → API → publishable key |
| `API_URL` | Vercel env | `https://llms-text-justin-he.fly.dev` |
| `FLY_API_TOKEN` | GitHub Actions secrets | `fly tokens create deploy -a llms-text-justin-he` |
| `DIRECT_DATABASE_URL` | GitHub Actions secrets | The same string as `DATABASE_URL` |

The Fly deploy token is scoped to this app alone, not the whole account.

### Rotating

- **Supabase keys** — regenerate in the dashboard, then update Fly secrets and Vercel env
- **Redis** — `upstash redis reset-password --db-id <id>`, then re-set `REDIS_URL`
- **Fly token** — `fly tokens revoke <id>`, re-create, `gh secret set FLY_API_TOKEN`

Retrieve and pipe in one step so the value never lands in a file or your shell history:

```bash
upstash redis get --db-id <id> \
  | jq -r '"rediss://default:\(.password)@\(.endpoint):\(.port)"' \
  | xargs -I{} fly secrets set REDIS_URL={} --app llms-text-justin-he
```

### Three things that will trip you up

1. **Use the Supabase session pooler on port 5432.** Not the direct connection (IPv6-only —
   GitHub runners can't reach it, and it's what makes Supabase offer a paid IPv4 add-on you
   don't need), and not port 6543 (transaction mode breaks asyncpg's prepared statements).
2. **The Vercel CLI defaults to the `dori` scope on this machine.** Every `vercel` command
   touching this project needs `--scope justinhe16s-projects`, or you'll modify the wrong team.
3. **Fly secrets read `Staged` until the first deploy.** That's expected — they were set with
   `--stage` and the app has no machines yet. CI's first deploy applies them.

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
