#!/usr/bin/env bash
# install-loop-eng.sh - loop-engineering toolkit (patterns + CLIs), mac twin.
#   bash harness/loop-engineering/install-loop-eng.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENDOR="$ROOT/harness/loop-engineering/vendor"
source "$ROOT/VERSIONS.lock"
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
ARTIFACT_MANIFEST="$DEPENDENCY_CACHE/manifest.json"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }

"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" preflight-source \
  --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
  --artifact-id source-loop-engineering --destination "$VENDOR" --trusted-root "$ROOT" \
  --expected-version "$LOOP_ENG_COMMIT" \
  --expected-requested-version "$LOOP_ENG_PIN" >/dev/null
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" install-source \
  --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
  --artifact-id source-loop-engineering --destination "$VENDOR" --trusted-root "$ROOT" \
  --expected-version "$LOOP_ENG_COMMIT" \
  --expected-requested-version "$LOOP_ENG_PIN" >/dev/null
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" validate-source \
  --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
  --artifact-id source-loop-engineering --destination "$VENDOR" --trusted-root "$ROOT" \
  --expected-version "$LOOP_ENG_COMMIT" \
  --expected-requested-version "$LOOP_ENG_PIN" >/dev/null

echo "==> loop CLIs (pinned, repo-local npm prefix)"
export npm_config_prefix="$ROOT/.tools/npm"
export npm_config_cache="$DEPENDENCY_CACHE/npm"
export npm_config_offline=true
mkdir -p "$npm_config_prefix"
artifact_path() {
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" artifact-path \
    --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$1" --expected-version "$2" \
    --expected-requested-version "$2" --root "$ROOT" --reproducible
}
LOOP_AUDIT_ARCHIVE="$(artifact_path npm-loop-audit "$LOOP_AUDIT_NPM")"
LOOP_INIT_ARCHIVE="$(artifact_path npm-loop-init "$LOOP_INIT_NPM")"
LOOP_COST_ARCHIVE="$(artifact_path npm-loop-cost "$LOOP_COST_NPM")"
LOOP_SYNC_ARCHIVE="$(artifact_path npm-loop-sync "$LOOP_SYNC_NPM")"
npm install -g --offline --no-audit --no-fund \
  "$LOOP_AUDIT_ARCHIVE" "$LOOP_INIT_ARCHIVE" \
  "$LOOP_COST_ARCHIVE" "$LOOP_SYNC_ARCHIVE" >/dev/null

bash "$ROOT/bootstrap/sync-skills.sh"
echo "==> loop-engineering installed: patterns in harness/loop-engineering/vendor, CLIs in .tools/npm"
echo "    try: oracle loops audit"
