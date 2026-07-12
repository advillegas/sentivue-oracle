#!/usr/bin/env bash
# Generate model configs atomically without changing tracked templates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ORACLE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
HOME_ROOT="${ORACLE_HOME:-$HOME}"
PYTHON_BIN=""

if [[ -n "${ORACLE_PYTHON:-}" ]] &&
   "$ORACLE_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
  PYTHON_BIN="$ORACLE_PYTHON"
else
  for candidate in "$ROOT/env/.venv/bin/python" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || { echo "sync-models: Python 3.12 or newer is required" >&2; exit 127; }

exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" sync-config \
  --root "$ROOT" --home "$HOME_ROOT"
