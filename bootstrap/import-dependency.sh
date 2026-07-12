#!/usr/bin/env bash
# Import an offline archive whose source identity and SHA-256 are already bound
# by VERSIONS.lock and verification/policy.json.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_ID="${1:?usage: import-dependency.sh ID FILE URL REQUESTED RESOLVED}"
SOURCE_FILE="${2:?usage: import-dependency.sh ID FILE URL REQUESTED RESOLVED}"
SOURCE_URL="${3:?usage: import-dependency.sh ID FILE URL REQUESTED RESOLVED}"
REQUESTED="${4:?usage: import-dependency.sh ID FILE URL REQUESTED RESOLVED}"
RESOLVED="${5:?usage: import-dependency.sh ID FILE URL REQUESTED RESOLVED}"
CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }

exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" import-artifact \
  --root "$ROOT" --cache "$CACHE" --artifact-id "$ARTIFACT_ID" \
  --file "$SOURCE_FILE" --url "$SOURCE_URL" \
  --requested-version "$REQUESTED" --resolved-version "$RESOLVED"
