#!/usr/bin/env bash
# Connected installer boundary: download only committed, checksum-bound inputs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
RETRIES=3
INCLUDE_OPTIONAL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cache) CACHE="${2:?--cache needs a path}"; shift ;;
    --retries) RETRIES="${2:?--retries needs a count}"; shift ;;
    --include-optional) INCLUDE_OPTIONAL=1 ;;
    *) echo "usage: acquire-dependencies.sh [--cache DIR] [--retries N] [--include-optional]" >&2; exit 2 ;;
  esac
  shift
done

PYTHON_BIN="${ORACLE_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in "$ROOT/env/.venv/bin/python" \
      "$ROOT/.tools/python-bootstrap/bin/python3" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
         >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || {
  echo "connected dependency acquisition requires Python 3.12 or newer" >&2
  exit 127
}

args=(
  acquire-dependencies
  --root "$ROOT"
  --cache "$CACHE"
  --platform darwin-arm64
  --retries "$RETRIES"
)
[[ "$INCLUDE_OPTIONAL" -eq 0 ]] || args+=(--include-optional)
exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" "${args[@]}"
