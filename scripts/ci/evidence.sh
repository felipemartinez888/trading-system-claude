#!/usr/bin/env bash
# Exact-commit CI evidence check (read-only). See scripts/ci/evidence.py for the rules.
# Usage: scripts/ci/evidence.sh <commit> [--repo owner/name] [--no-logs]
# Exit 0: a qualifying run exists for exactly that commit; 1: none; 2: API not readable.
set -uo pipefail
exec python3 "$(dirname "$0")/evidence.py" "$@"
