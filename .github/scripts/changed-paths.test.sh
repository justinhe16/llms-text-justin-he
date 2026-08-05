#!/usr/bin/env bash
#
# Self-test for changed-paths.sh.
#
# The path filter is the only thing standing between a change and the jobs that verify
# it: if it wrongly answers "false", CI reports green without having run the tests. That
# is a failure mode with no other alarm on it, so the matcher gets its own test, and both
# workflows run this file before they trust its answer. It takes about a fifth of a
# second and needs no network.
#
#     bash .github/scripts/changed-paths.test.sh

set -euo pipefail

script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/changed-paths.sh"
failures=0

# expect <want> <files> <pattern>...
expect() {
  local want="$1" files="$2"
  shift 2
  local got
  got="$(CHANGED_FILES="$files" bash "$script" "$@" 2>/dev/null)"
  if [ "$got" = "$want" ]; then
    printf 'ok    %-9s %-34s <- %s\n' "$want" "$*" "$(echo "$files" | tr '\n' ' ')"
  else
    printf 'FAIL  want=%-4s got=%-4s %-28s <- %s\n' "$want" "$got" "$*" \
      "$(echo "$files" | tr '\n' ' ')"
    failures=$((failures + 1))
  fi
}

backend_patterns=('backend/**' 'db/**' '.github/workflows/ci-backend.yml'
  '.github/scripts/changed-paths.sh' '.github/scripts/changed-paths.test.sh')
frontend_patterns=('frontend/**' '.github/workflows/ci-frontend.yml'
  '.github/scripts/changed-paths.sh' '.github/scripts/changed-paths.test.sh')

# A backend-only change runs backend CI and not frontend CI.
expect true 'backend/app/main.py' "${backend_patterns[@]}"
expect false 'backend/app/main.py' "${frontend_patterns[@]}"

# A frontend-only change runs frontend CI and not backend CI.
expect true 'frontend/app/page.tsx' "${frontend_patterns[@]}"
expect false 'frontend/app/page.tsx' "${backend_patterns[@]}"

# A schema change re-runs the backend tests that depend on it.
expect true 'db/schema.prisma' "${backend_patterns[@]}"
expect true 'db/migrations/20260101_init/migration.sql' "${backend_patterns[@]}"
expect false 'db/schema.prisma' "${frontend_patterns[@]}"

# A docs-only change runs neither. This is the case that would deadlock a required
# check filtered at the workflow trigger, and the reason the gate jobs exist.
expect false 'README.md' "${backend_patterns[@]}"
expect false 'README.md' "${frontend_patterns[@]}"
expect false $'README.md\nARCHITECTURE.md\nCLAUDE.md' "${backend_patterns[@]}"
expect false $'README.md\nARCHITECTURE.md\nCLAUDE.md' "${frontend_patterns[@]}"

# A workflow edits itself, and the shared filter re-runs both.
expect true '.github/workflows/ci-backend.yml' "${backend_patterns[@]}"
expect false '.github/workflows/ci-backend.yml' "${frontend_patterns[@]}"
expect true '.github/scripts/changed-paths.sh' "${backend_patterns[@]}"
expect true '.github/scripts/changed-paths.sh' "${frontend_patterns[@]}"

# A mixed change runs both.
expect true $'backend/app/main.py\nfrontend/app/page.tsx' "${backend_patterns[@]}"
expect true $'backend/app/main.py\nfrontend/app/page.tsx' "${frontend_patterns[@]}"

# Prefix collisions must not match: `backend-notes.md` is not inside `backend/`.
expect false 'backend-notes.md' "${backend_patterns[@]}"
expect false 'docs/frontend/readme.md' "${frontend_patterns[@]}"

if [ "$failures" -ne 0 ]; then
  echo "changed-paths.test.sh: ${failures} failing case(s)" >&2
  exit 1
fi
echo "changed-paths.test.sh: all cases pass"
