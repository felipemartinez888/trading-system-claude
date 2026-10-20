#!/usr/bin/env bash
# Emit the last lines of each CI step log as one ::error annotation per step.
set -uo pipefail
dir="${GITHUB_WORKSPACE:-$(pwd)}/ci-logs"
[ -d "$dir" ] || exit 0
for f in "$dir"/*.log; do
  [ -f "$f" ] || continue
  body=$(tail -n 80 "$f" | sed -e 's/%/%25/g' -e 's/\r//g' | awk 'BEGIN{ORS="%0A"} {print}')
  echo "::error title=$(basename "$f" .log) (${RUNNER_OS:-?})::${body}"
done
exit 0
