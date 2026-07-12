#!/usr/bin/env bash
# Build and validate every local release artifact before optional publication.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION=""
REVISION="HEAD"
OUTPUT=""
PUBLISH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift ;;
    --revision) REVISION="${2:-}"; shift ;;
    --output) OUTPUT="${2:-}"; shift ;;
    --publish) PUBLISH=1 ;;
    *) echo "usage: release.sh --version vX.Y.Z [--revision COMMIT] [--output DIR] [--publish]" >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$VERSION" ]] || { echo "release: --version is required" >&2; exit 2; }
[[ -n "$OUTPUT" ]] || OUTPUT="$ROOT/artifacts/releases/$VERSION"

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
[[ -n "$PYTHON_BIN" ]] || { echo "release: Python 3.12 or newer is required" >&2; exit 127; }

ARGS=(release --root "$ROOT" --version "$VERSION" --revision "$REVISION" --output "$OUTPUT")
if [[ "$PUBLISH" -eq 1 ]]; then ARGS+=(--publish)
else ARGS+=(--preflight-only)
fi
exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" "${ARGS[@]}"
