#!/usr/bin/env bash
# Run a CI command, mirroring its output into ci-logs/<name>.log so that, on failure,
# annotate.sh can surface the tail as check-run annotations (readable via the GitHub API
# even where raw log downloads are not reachable).
set -uo pipefail
name="$1"; shift
dir="${GITHUB_WORKSPACE:-$(pwd)}/ci-logs"
mkdir -p "$dir"
"$@" 2>&1 | tee "$dir/$name.log"
exit "${PIPESTATUS[0]}"
