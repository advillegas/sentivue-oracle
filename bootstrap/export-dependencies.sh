#!/usr/bin/env bash
# Fetch one dependency into an explicit cache with exact provenance and SHA-256.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE="$ROOT/incoming/dependency-cache"
ARTIFACT_ID=""
URL=""
REQUESTED=""
RESOLVED=""
EXPECTED=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifact-id) ARTIFACT_ID="${2:-}"; shift ;;
    --url) URL="${2:-}"; shift ;;
    --requested-version) REQUESTED="${2:-}"; shift ;;
    --resolved-version) RESOLVED="${2:-}"; shift ;;
    --expected-sha256) EXPECTED="${2:-}"; shift ;;
    --cache) CACHE="${2:-}"; shift ;;
    --root) ROOT="${2:-}"; shift ;;
    *) echo "usage: export-dependencies.sh --artifact-id ID --url URL --requested-version PIN --resolved-version EXACT [--expected-sha256 HASH] [--cache DIR]" >&2; exit 2 ;;
  esac
  shift
done

[[ -n "$ARTIFACT_ID" && -n "$URL" && -n "$REQUESTED" && -n "$RESOLVED" ]] || {
  echo "dependency export: artifact id, URL, requested version, and resolved version are required" >&2
  exit 2
}

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
[[ -n "$PYTHON_BIN" ]] || { echo "dependency export: Python 3.12 or newer is required" >&2; exit 127; }

ARGS=(export-artifact --cache "$CACHE" --artifact-id "$ARTIFACT_ID" --url "$URL"
  --requested-version "$REQUESTED" --resolved-version "$RESOLVED")
[[ -z "$EXPECTED" ]] || ARGS+=(--expected-sha256 "$EXPECTED")
exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" "${ARGS[@]}"
