#!/usr/bin/env bash
# Trading System Claude - historical data import (owner machine; same steps as import_history.ps1).
# Usage: scripts/import_history.sh [--from 2022-01-01] [--skip-universe]
set -uo pipefail
cd "$(dirname "$0")/.."

FROM=2022-01-01
SKIP_UNIVERSE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --skip-universe) SKIP_UNIVERSE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || { echo "uv is not installed (see scripts/README.md)" >&2; exit 1; }

TODAY=$(date -u +%Y-%m-%d)
LAST_COMPLETE=$(uv run python -c 'import datetime as d; t=d.datetime.now(d.UTC).date().replace(day=1); print(t-d.timedelta(days=1))')
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="evidence/data/$STAMP"
mkdir -p "$OUT"
STEPS=()
FAILED=0

step() {
  local name="$1"; shift
  echo "==> $name"
  uv run tsclaude "$@"
  local code=$?
  STEPS+=("{\"step\": \"$name\", \"exit_code\": $code}")
  if [[ $code -ne 0 ]]; then echo "    step '$name' exited with $code" >&2; FAILED=$((FAILED+1)); fi
  return $code
}

SOURCES=(configs/universe.yaml)
if [[ $SKIP_UNIVERSE -eq 0 ]]; then
  if step universe data universe --snapshot "$TODAY" --summary "$OUT/universe.json" \
      && [[ -f "configs/universe_snapshot_$TODAY.yaml" ]]; then
    SOURCES+=("configs/universe_snapshot_$TODAY.yaml")
  fi
fi

for src in "${SOURCES[@]}"; do
  name=$(basename "$src" .yaml)
  for market in spot usdm; do
    step "import-$market-$name" data import --market "$market" --symbols-from "$src" \
      --from "$FROM" --to "$LAST_COMPLETE" --summary "$OUT/import-$market-$name.json"
  done
done

step verify data verify --all --summary "$OUT/verify.json"

{
  echo "{\"from\": \"$FROM\", \"to\": \"$LAST_COMPLETE\", \"failed_steps\": $FAILED, \"steps\": ["
  (IFS=,; echo "${STEPS[*]}")
  echo "]}"
} > "$OUT/summary.json"
echo "Summary written to $OUT/summary.json (failed steps: $FAILED)"
[[ $FAILED -eq 0 ]]
