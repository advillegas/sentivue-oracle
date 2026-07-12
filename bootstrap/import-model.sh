#!/usr/bin/env bash
# Import local shards only after their expected identities were independently
# promoted into serving/model-authorities.json.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_NAME="${1:?usage: import-model.sh MODEL AUTHORITY.json [MODELS_ROOT]}"
AUTHORITY_FILE="${2:?usage: import-model.sh MODEL AUTHORITY.json [MODELS_ROOT]}"
MODELS_ROOT="${3:-$ROOT/models}"
CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }

exec "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" import-model \
  --root "$ROOT" --cache "$CACHE" --models-root "$MODELS_ROOT" \
  --model-name "$MODEL_NAME" --authority "$AUTHORITY_FILE"
