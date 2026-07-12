#!/usr/bin/env bash
# Thin POSIX entry point for the shared Python verifier.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN=""
if [[ -n "${ORACLE_PYTHON:-}" ]] &&
   "$ORACLE_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
  PYTHON_BIN="$ORACLE_PYTHON"
else
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "verification: Python 3.12 or newer is required" >&2
  exit 127
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/verify.py" --root "$ROOT" "$@"
