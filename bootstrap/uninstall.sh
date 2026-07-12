#!/usr/bin/env bash
# Ownership-scoped uninstaller. Default behavior is a read-only dry run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOME_ROOT="${ORACLE_HOME:-$HOME}"
APPLY=0
PURGE=0
CONFIRM_PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --purge) PURGE=1 ;;
    --confirm-purge) CONFIRM_PURGE=1 ;;
    --root) ROOT="${2:-}"; shift ;;
    --home) HOME_ROOT="${2:-}"; shift ;;
    *) echo "usage: uninstall.sh [--apply] [--purge --confirm-purge] [--root DIR] [--home DIR]" >&2; exit 2 ;;
  esac
  shift
done

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
[[ -n "$PYTHON_BIN" ]] || { echo "uninstall: Python 3.12 or newer is required" >&2; exit 127; }

ARGS=(uninstall --root "$ROOT" --home "$HOME_ROOT")
[[ "$APPLY" -eq 1 ]] && ARGS+=(--apply)
[[ "$PURGE" -eq 1 ]] && ARGS+=(--purge)
[[ "$CONFIRM_PURGE" -eq 1 ]] && ARGS+=(--confirm-purge)
exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" "${ARGS[@]}"
