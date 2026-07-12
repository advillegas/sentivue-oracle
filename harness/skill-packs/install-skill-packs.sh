#!/usr/bin/env bash
# install-skill-packs.sh - mac twin: vendor pinned superpowers + gstack and
# sync their skills into both engines (prefixes 'sp-' / 'gs-').
#   bash harness/skill-packs/install-skill-packs.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/harness/skill-packs"
source "$ROOT/VERSIONS.lock"
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
ARTIFACT_MANIFEST="$DEPENDENCY_CACHE/manifest.json"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }
CC="$ROOT/engines/claude-code/home/skills"
OC="$ROOT/engines/opencode/xdg/opencode/skill"
mkdir -p "$CC" "$OC" "$HERE/vendor"

vendored() {  # vendored <name> <artifact-id> <requested> <resolved>
  local name="$1" artifact_id="$2" requested="$3" resolved="$4" v="$HERE/vendor/$1"
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" install-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$v" \
    --expected-version "$resolved" --expected-requested-version "$requested"
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" validate-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$v" \
    --expected-version "$resolved" --expected-requested-version "$requested" >/dev/null
  echo "==> $name policy-bound vendor tree installed ($resolved)" >&2
  echo "$v"
}

sync_pack() {  # sync_pack <vendor-dir> <prefix> <skills-root>
  local vendor="$1" prefix="$2" base="$1/$3" count=0
  [[ -d "$base" ]] || return 0
  for dir in "$base"/*/; do
    [[ -f "$dir/SKILL.md" ]] || continue
    local name; name="$(basename "$dir")"
    ln -sfn "${dir%/}" "$CC/$prefix-$name"
    ln -sfn "${dir%/}" "$OC/$prefix-$name"
    count=$((count+1))
  done
  echo "$count"
}

SP="$(vendored superpowers source-superpowers "$SUPERPOWERS_PIN" "$SUPERPOWERS_COMMIT")"
echo "==> superpowers: $(sync_pack "$SP" sp skills) skills synced (prefix 'sp-')"
GS="$(vendored gstack source-gstack "$GSTACK_PIN" "$GSTACK_COMMIT")"
echo "==> gstack: $(sync_pack "$GS" gs .) skills synced (prefix 'gs-')"
echo "Both packs live in harness/skill-packs/vendor; re-run after changing pins."
