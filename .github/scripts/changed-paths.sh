#!/usr/bin/env bash
#
# Print "true" if any file changed by the current GitHub Actions event matches one of the
# glob patterns given as arguments, and "false" otherwise.
#
#     .github/scripts/changed-paths.sh 'backend/**' 'db/**'
#
# WHY THIS EXISTS INSTEAD OF A `paths:` FILTER ON THE WORKFLOW TRIGGER
#
# A workflow that does not trigger reports no checks at all. Branch protection on `main`
# requires the `backend-ci` and `frontend-ci` checks, and a required check that never
# reports blocks the merge *forever* — so a README-only pull request, which matches
# neither path list, would be unmergeable. Filtering here instead means the workflow
# always triggers and always reports its gate job, while the expensive jobs (pytest, a
# Postgres container, `next build`) still never run on a pull request that cannot affect
# them. See README.md "CI" for the full reasoning.
#
# The cost of this choice is one ~10 second runner job per workflow per pull request. The
# alternative — a second workflow with the complementary `paths-ignore` list and a
# same-named no-op job — keeps that cost but duplicates every path list, and the day the
# two copies drift the required check either reports twice or not at all. One list, in
# one file, is worth ten seconds.
#
# FAIL-OPEN
#
# If the set of changed files cannot be determined, everything is treated as changed. CI
# that does too much is a nuisance; CI that silently skips the tests for a change it
# could not classify is a defect.
#
# TESTING
#
# Set CHANGED_FILES to a newline-separated list to exercise the matcher without a GitHub
# event — used by .github/scripts/changed-paths.test.sh. The workflows never set it.

set -euo pipefail

patterns=("$@")
if [ ${#patterns[@]} -eq 0 ]; then
  echo "changed-paths.sh: at least one glob pattern is required" >&2
  exit 2
fi

# Emit the changed files one per line, or return non-zero if they cannot be determined.
changed_files() {
  if [ -n "${CHANGED_FILES:-}" ]; then
    printf '%s\n' "$CHANGED_FILES"
    return 0
  fi

  case "${GITHUB_EVENT_NAME:-}" in
    pull_request)
      # The API answer is exactly the pull request's file list, with no dependence on
      # how deep the checkout was cloned or on which merge commit was checked out.
      [ -n "${PR_NUMBER:-}" ] || return 1
      # Both sides of a rename count: moving backend/x.py to frontend/x.py changes the
      # backend as surely as deleting it would, and only `previous_filename` says so.
      gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/files" \
        --jq '.[] | .filename, (.previous_filename // empty)'
      ;;
    push)
      # `github.event.before` is all zeros when the branch was just created, and can
      # point at a commit that no longer exists after a force push.
      case "${BEFORE_SHA:-}" in
        "" | 0000000000000000000000000000000000000000) return 1 ;;
      esac
      git cat-file -e "${BEFORE_SHA}^{commit}" 2>/dev/null || return 1
      git diff --name-only "${BEFORE_SHA}" "${GITHUB_SHA}"
      ;;
    *)
      return 1
      ;;
  esac
}

if ! files="$(changed_files)"; then
  echo "changed-paths.sh: could not determine the changed files; treating all as changed" >&2
  echo "true"
  exit 0
fi

matched=""
while IFS= read -r file; do
  [ -n "$file" ] || continue
  for pattern in "${patterns[@]}"; do
    # The right-hand side is an unquoted glob on purpose. Inside [[ ]] a `*` matches `/`
    # as well, so 'backend/**' matches backend/app/main.py at any depth.
    # shellcheck disable=SC2053
    if [[ "$file" == $pattern ]]; then
      matched="$file"
      break 2
    fi
  done
done <<<"$files"

if [ -n "$matched" ]; then
  echo "changed-paths.sh: '${matched}' matches ${patterns[*]}" >&2
  echo "true"
else
  echo "changed-paths.sh: nothing matches ${patterns[*]}" >&2
  echo "false"
fi
