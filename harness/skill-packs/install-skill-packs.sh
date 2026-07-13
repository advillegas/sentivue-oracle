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
OFFLINE_POLICY="$HERE/offline-policy.json"
SERVING="$ROOT/verification/serving.py"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }
CC="$ROOT/engines/claude-code/home/skills"
OC="$ROOT/engines/opencode/xdg/opencode/skill"
mkdir -p "$CC" "$OC" "$HERE/vendor"

vendored() {  # vendored <name> <artifact-id> <requested> <resolved>
  local name="$1" artifact_id="$2" requested="$3" resolved="$4" v="$HERE/vendor/$1"
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" preflight-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$v" --trusted-root "$ROOT" \
    --expected-version "$resolved" --expected-requested-version "$requested"
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" install-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$v" --trusted-root "$ROOT" \
    --expected-version "$resolved" --expected-requested-version "$requested"
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" validate-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$v" --trusted-root "$ROOT" \
    --expected-version "$resolved" --expected-requested-version "$requested" >/dev/null
  echo "==> $name policy-bound vendor tree installed ($resolved)" >&2
  echo "$v"
}

vendored superpowers source-superpowers "$SUPERPOWERS_PIN" "$SUPERPOWERS_COMMIT" >/dev/null
vendored gstack source-gstack "$GSTACK_PIN" "$GSTACK_COMMIT" >/dev/null

set +e
AUDIT="$("$PYTHON_BIN" "$SERVING" skill-policy --vendor "$HERE/vendor" \
  --policy "$OFFLINE_POLICY" --format json 2>&1)"
AUDIT_EXIT=$?
set -e
[[ $AUDIT_EXIT -eq 0 || $AUDIT_EXIT -eq 2 ]] || {
  echo "ERROR: third-party skill policy inspection failed: $AUDIT" >&2
  exit 1
}
printf '%s' "$AUDIT" | jq -r \
  '.flagged[] | "WARN: excluded \(.name): \(.reason)"' >&2

for destination in "$CC" "$OC"; do
  for stale in "$destination"/sp-* "$destination"/gs-*; do
    [[ -e "$stale" || -L "$stale" ]] || continue
    rm -rf "$stale"
  done
done

COUNT=0
while IFS=$'\t' read -r name source; do
  [[ -n "$name" && -f "$source/SKILL.md" ]] || {
    echo "ERROR: allowed skill path is incomplete: $source" >&2
    exit 1
  }
  ln -sfn "$source" "$CC/$name"
  ln -sfn "$source" "$OC/$name"
  COUNT=$((COUNT+1))
done < <(printf '%s' "$AUDIT" | jq -r '.allowed[] | [.name, .path] | @tsv')

echo "==> offline-curated skills synced: $COUNT"
echo "Flagged network-capable instructions remain in vendor quarantine only."
