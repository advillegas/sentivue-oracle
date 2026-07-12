#!/usr/bin/env bash
# Promote independently verified dependency identity/digest metadata.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_ID="${1:?usage: promote-dependency.sh ID AUTHORITY.json}"
AUTHORITY_FILE="${2:?usage: promote-dependency.sh ID AUTHORITY.json}"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }

exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" promote-authority \
  --root "$ROOT" --artifact-id "$ARTIFACT_ID" --authority "$AUTHORITY_FILE"
