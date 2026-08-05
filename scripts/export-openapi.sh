#!/usr/bin/env bash
# scripts/export-openapi.sh — regenerate frontend/lib/api/openapi.json from
# `app.openapi()`, or (--check) prove the committed snapshot still matches what the live
# schema would produce.
#
# Usage:
#   scripts/export-openapi.sh            Write frontend/lib/api/openapi.json.
#   scripts/export-openapi.sh --check    Exit 1 if the committed snapshot has drifted.
#
# This is the backend half of the drift check described in ARCHITECTURE.md §8.6: the
# frontend half (.github/workflows/ci-frontend.yml) regenerates lib/api/schema.d.ts from
# the *committed* lib/api/openapi.json and fails on a diff, which only catches a hand-edit
# of the generated TypeScript. This script is what proves that JSON itself still matches
# the live FastAPI schema — the check `make lint` and .github/workflows/ci-backend.yml's
# `lint` job both run.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$script_dir/.."
backend_dir="$repo_root/backend"
snapshot="$repo_root/frontend/lib/api/openapi.json"

# backend/app/core/settings.py validates these four at *import* time (see its own
# docstring): both processes that boot from the backend image need to fail fast on a
# missing credential rather than serve a 500 later. That check runs the instant
# backend/scripts/export_openapi.py does `from app.main import app` — whether or not this
# machine has ever seen a real Supabase project or database — even though the OpenAPI
# document itself is built entirely from routes, `response_model`s, and `Depends()`
# wiring already in memory, and depends on none of these four values.
#
# `:=` only fills a variable that is unset or empty, so this works unmodified both on a
# laptop with no backend/.env and in ci-backend.yml's `lint` job, which already exports
# its own throwaway values for the same four names. Every default below is an
# UNMISTAKABLE non-value — never a real credential, per ARCHITECTURE.md §9 — chosen so
# that nobody could mistake one for a working connection string or key if it ever ended
# up in a log.
: "${DATABASE_URL:=postgresql://openapi-export:not-a-real-password@localhost/openapi-export}"
: "${REDIS_URL:=redis://localhost:6379/0}"
: "${SUPABASE_URL:=https://openapi-export.invalid}"
: "${SUPABASE_SECRET_KEY:=not-a-real-secret-key}"
export DATABASE_URL REDIS_URL SUPABASE_URL SUPABASE_SECRET_KEY

# Prefer the project's own virtualenv, so a laptop that has run `make setup` exercises the
# exact interpreter and pinned dependencies `make lint` does. ci-backend.yml's `lint` job
# has no venv — it installs backend/requirements-dev.txt straight into the runner's system
# Python — so falling back to `python3` there is the correct behavior, not a compromise.
if [ -x "$backend_dir/.venv/bin/python" ]; then
  python="$backend_dir/.venv/bin/python"
else
  python="python3"
fi

usage() {
  echo "Usage: $(basename "$0") [--check]" >&2
  exit 2
}

mode="write"
case "${1:-}" in
  "") ;;
  --check) mode="check" ;;
  *) usage ;;
esac

# Run from backend/, as `-m scripts.export_openapi` rather than
# `python scripts/export_openapi.py`, so Python resolves `from app.main import app`
# exactly the way the application itself does: `-m` puts the current directory
# (backend/) on sys.path, not backend/scripts/.
export_document() {
  (cd "$backend_dir" && "$python" -m scripts.export_openapi)
}

if [ "$mode" = "write" ]; then
  export_document >"$snapshot"
  echo "export-openapi: wrote $snapshot" >&2
  exit 0
fi

# --check mode: generate into a throwaway file and diff it against the committed
# snapshot, so the working tree is untouched whether or not this passes.
tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
export_document >"$tmp_file"

if diff -u "$snapshot" "$tmp_file"; then
  echo "export-openapi: $snapshot matches the live schema" >&2
  exit 0
fi

echo "export-openapi: frontend/lib/api/openapi.json is out of date." >&2
echo "export-openapi: run \`make openapi\` from the repo root and commit the result." >&2
exit 1
