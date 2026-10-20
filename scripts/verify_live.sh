#!/usr/bin/env bash
# M1 gate evidence: run the app with monitoring auto-started, then write a JSON report.
# Usage: scripts/verify_live.sh [--hours 2] [--no-browser]
# The report is written to evidence/live/verify-<UTC>.json (see scripts/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "uv is not installed; see scripts/README.md" >&2; exit 1; }
frozen=()
[ -f uv.lock ] && frozen=(--frozen)
exec uv run "${frozen[@]}" python scripts/verify_live.py "$@"
