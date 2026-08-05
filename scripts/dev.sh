#!/usr/bin/env bash
# scripts/dev.sh — process runner for `make dev`.
#
# ARCHITECTURE.md §2 forbids a root package manifest and monorepo tooling, so there is no
# `concurrently` here (and `npx --yes concurrently` would need network access anyway).
# This script starts local infra (Supabase, Redis), then the backend API, then the ARQ
# worker and the Next.js frontend when they exist, each with a colored, named log prefix,
# and tears the whole group of processes it started down cleanly on Ctrl-C.
#
# Run through `make dev`, not directly — the Makefile builds backend/.venv first.
#
# Env vars this script honors:
#   VENV_DIR       Path to the backend virtualenv.       default: backend/.venv
#   WORKER_MODULE  ARQ worker settings module path.       default: app.worker.settings.WorkerSettings
#   API_HOST       Interface the API binds.               default: 127.0.0.1
#   API_PORT       Port the API binds.                    default: 8000
#   NO_COLOR       Any non-empty value disables prefix colors (https://no-color.org).

set -eu

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

venv_dir="${VENV_DIR:-backend/.venv}"
worker_module="${WORKER_MODULE:-app.worker.settings.WorkerSettings}"

# Loopback by default, matching the deliberately loopback-only Redis binding in
# docker-compose.yml: nothing in this project needs the dev API reachable from the LAN.
# Both are overridable so a port already in use can be worked around without editing this
# file (README.md > Run locally > Troubleshooting).
api_host="${API_HOST:-127.0.0.1}"
api_port="${API_PORT:-8000}"

# --- color handling --------------------------------------------------------------
# Disabled when stdout is not a TTY (e.g. piped to a file or CI) or when NO_COLOR is set.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  c_api=$'\033[36m'
  c_worker=$'\033[35m'
  c_frontend=$'\033[33m'
  c_reset=$'\033[0m'
else
  c_api=""
  c_worker=""
  c_frontend=""
  c_reset=""
fi

# Space-separated list of PIDs this script has started, for teardown. Not a bash array —
# the default `env bash` on this platform is old enough (3.2) that empty-array expansion
# under `set -u` is unreliable, and a plain word list needs no such guard.
pids=""

# Runs "$@" in the background with every line of its combined stdout/stderr prefixed by a
# colored "[name] " tag, and records its PID for teardown().
#
# $! right after this backgrounds "$@" is the PID of "$@" itself, not of some wrapping
# subshell: the prefixing lives in a `>( )` process substitution, which is a *separate*
# process that reads until "$@" closes its output — it exits on its own once "$@" does,
# so signaling the one PID we record is enough to leave nothing orphaned.
run_prefixed() {
  name="$1"
  color="$2"
  shift 2
  "$@" > >(sed -u "s/^/${color}[${name}]${c_reset} /") 2>&1 &
  pids="$pids $!"
}

teardown() {
  trap - INT TERM EXIT
  echo ""
  echo "==> stopping dev processes"
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in $pids; do
    wait "$pid" 2>/dev/null || true
  done
  echo "==> dev processes stopped"
  echo "==> Supabase and Redis containers are still running — \`make down\` stops those"
  # Ctrl-C is how this script is *meant* to end, so a clean teardown reports success.
  # Without this the shell would exit 130 and make would print "*** [dev] Error 130",
  # which reads like a failure for what is the documented way to stop `make dev`.
  exit 0
}
trap teardown INT TERM

# --- infra: Supabase --------------------------------------------------------------
echo "==> starting Supabase (Postgres, Auth, Storage)"
if ! supabase start >/dev/null; then
  echo "dev: \`supabase start\` failed — see output above" >&2
  exit 1
fi

# --- infra: Redis -------------------------------------------------------------------
echo "==> starting Redis"
docker compose up -d redis >/dev/null

redis_container="$(docker compose ps -q redis)"
redis_ready=0
attempt=0
while [ "$attempt" -lt 30 ]; do
  status="$(docker inspect --format '{{.State.Health.Status}}' "$redis_container" 2>/dev/null || echo "")"
  if [ "$status" = "healthy" ]; then
    redis_ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
if [ "$redis_ready" -ne 1 ]; then
  echo "dev: redis did not report healthy in time" >&2
  exit 1
fi

# Bridges `supabase status` into DATABASE_URL / SUPABASE_URL / SUPABASE_SECRET_KEY /
# REDIS_URL for the API and worker below, without ever printing a value (see
# scripts/local-env.sh and ARCHITECTURE.md §9.4).
#
# Captured into a variable *before* `eval` on purpose. `eval "$(cmd)"` would discard cmd's
# exit status even under `set -e` — a failing local-env.sh would print its error and this
# script would carry on and boot the API against an empty configuration, which is exactly
# the "half-parsed configuration leaking downstream" that local-env.sh promises to prevent.
# Assigning from a command substitution does propagate the failure, so this stops here.
if ! env_exports="$(bash scripts/local-env.sh export)"; then
  echo "dev: could not read the local Supabase configuration — see the error above" >&2
  exit 1
fi
eval "$env_exports"
export DATABASE_URL DIRECT_DATABASE_URL SUPABASE_URL SUPABASE_SECRET_KEY REDIS_URL

# --- API ------------------------------------------------------------------------------
run_prefixed api "$c_api" "$venv_dir/bin/python" -m uvicorn app.main:app \
  --app-dir backend --host "$api_host" --port "$api_port" --reload --reload-dir backend/app

# --- worker ---------------------------------------------------------------------------
# No longer optional: the ARQ worker exists, so `make dev` runs it. It consumes from the
# docker-compose Redis started above over plain `redis://`, because TLS is decided by the
# URL scheme — the same WorkerSettings that reaches Upstash over `rediss://` in production
# works here unchanged (backend/app/infrastructure/queue/pool.py).
#
# This FAILS rather than skipping when `arq` is missing. The graceful skip that used to
# live here was right while the worker was unbuildable; keeping it now would let a stale
# virtualenv produce a dev environment with no queue consumer, in which enqueued jobs sit
# in Redis forever and the only symptom is that nothing happens.
if [ ! -x "$venv_dir/bin/arq" ]; then
  echo "dev: 'arq' is not installed in $venv_dir. Run 'make setup' to refresh it." >&2
  exit 1
fi
run_prefixed worker "$c_worker" env PYTHONPATH=backend "$venv_dir/bin/arq" "$worker_module"

# --- frontend (skipped when frontend/ is absent) --------------------------------------
if [ -d frontend ]; then
  # Generates frontend/.env.local from the running local Supabase stack (never
  # overwriting one a developer has hand-edited — see scripts/local-env.sh). Without
  # this, the frontend boots with NEXT_PUBLIC_SUPABASE_URL unset and its Supabase
  # client throws on first use. Same failure-propagation reasoning as env_exports
  # above: captured into an if-condition rather than run bare, so a failure here stops
  # this script instead of leaking a half-configured frontend downstream.
  if ! API_HOST="$api_host" API_PORT="$api_port" bash scripts/local-env.sh write-frontend-env; then
    echo "dev: could not write frontend/.env.local — see the error above" >&2
    exit 1
  fi
  run_prefixed frontend "$c_frontend" npm --prefix frontend run dev
else
  echo "frontend: skipped — no frontend/ in this checkout"
fi

cat <<EOF

llms-text dev environment is up:
  App              http://localhost:3000
  API              http://$api_host:$api_port
  API docs         http://$api_host:$api_port/docs
  Supabase Studio  http://localhost:54323

Press Ctrl-C to stop the processes above.
Supabase and Redis containers keep running — \`make down\` stops those.
EOF

wait
