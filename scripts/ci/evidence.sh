#!/usr/bin/env bash
# Exact-commit CI evidence check (read-only). See scripts/ci/evidence.py for the rules.
# Usage: scripts/ci/evidence.sh <commit> [--repo owner/name] [--no-logs]
# Exit 0: a qualifying run exists for exactly that commit; 1: none; 2: API not readable.
set -uo pipefail
py=$(command -v python3 || command -v python) || { echo "no python found" >&2; exit 2; }
exec "$py" "$(dirname "$0")/evidence.py" "$@"
