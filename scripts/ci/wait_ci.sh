#!/usr/bin/env bash
# Dev helper (sandbox): wait for the CI run of a commit, then print per-job results and the
# failure annotations (the only CI output readable from the sandbox).
# Usage: scripts/ci/wait_ci.sh <sha-prefix-or-branch> [repo]
set -uo pipefail
ref="${1:?ref}"
repo="${2:-felipemartinez888/trading-system-claude}"
sha=$(git rev-parse "$ref" 2>/dev/null || echo "$ref")
run=""
for _ in $(seq 1 120); do
  run=$(gh run list -R "$repo" --limit 20 --json databaseId,headSha,status \
        --jq ".[] | select(.headSha | startswith(\"$sha\")) | \"\(.databaseId) \(.status)\"" | head -1)
  if [[ "$run" == *completed ]]; then break; fi
  sleep 20
done
id="${run%% *}"
echo "run $id ($run)"
gh run view "$id" -R "$repo" --json jobs \
  --jq '.jobs[] | "\(.name): \(.conclusion) " + ([.steps[] | select(.conclusion=="failure") | .name] | join(","))'
for check in $(gh api "repos/$repo/commits/$sha/check-runs" --jq '.check_runs[] | select(.conclusion=="failure") | .id'); do
  gh api "repos/$repo/check-runs/$check/annotations" \
    --jq '.[] | "----- \(.title // "")\n\(.message)"'
done
