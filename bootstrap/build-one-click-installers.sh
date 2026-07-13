#!/usr/bin/env bash
# Build checksummed Windows and macOS one-click installers from an immutable commit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION=""
REVISION="HEAD"
OUTPUT=""
DEPENDENCY_CACHE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift ;;
    --revision) REVISION="${2:-}"; shift ;;
    --output) OUTPUT="${2:-}"; shift ;;
    --dependency-cache) DEPENDENCY_CACHE="${2:-}"; shift ;;
    *) echo "usage: build-one-click-installers.sh --version vX.Y.Z [--revision COMMIT] [--output DIR] [--dependency-cache DIR]" >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$VERSION" ]] || { echo "installer build: --version is required" >&2; exit 2; }
[[ -n "$OUTPUT" ]] || OUTPUT="$ROOT/artifacts/installers/$VERSION"

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
[[ -n "$PYTHON_BIN" ]] || { echo "installer build: Python 3.12 or newer is required" >&2; exit 127; }

ARGS=(installers --root "$ROOT" --version "$VERSION" --revision "$REVISION" --output "$OUTPUT")
[[ -z "$DEPENDENCY_CACHE" ]] || ARGS+=(--dependency-cache "$DEPENDENCY_CACHE")
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" "${ARGS[@]}"

if [[ "$(uname -s)" == "Darwin" ]]; then
  INSTALLER=""
  for candidate in "$OUTPUT"/*.command; do
    [[ -f "$candidate" ]] || continue
    INSTALLER="$candidate"
    break
  done
  [[ -n "$INSTALLER" ]] || {
    echo "installer build: generated macOS .command was not found" >&2
    exit 1
  }
  ORACLE_PYTHON="$PYTHON_BIN" bash "$ROOT/bootstrap/build-macos-package.sh" \
    --version "$VERSION" \
    --installer "$INSTALLER" \
    --output "${OUTPUT}-macos-package"
fi
