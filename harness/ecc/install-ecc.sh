#!/usr/bin/env bash
# Install a pinned, curated subset of ECC (harness skills) into both engines.
# ECC complements the engines: it is a config/skill layer, not an agent itself.
# NOTE: stock macOS bash is 3.2 - no associative arrays in this file.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/VERSIONS.lock"
VENDOR="$ROOT/harness/ecc/vendor"
PROFILE="$ROOT/harness/ecc/profile.txt"
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }
CC_DIR="$ROOT/engines/claude-code/home/skills"
OC_DIR="$ROOT/engines/opencode/xdg/opencode/skill"
mkdir -p "$CC_DIR" "$OC_DIR"

"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" install-source \
  --root "$ROOT" --manifest "$DEPENDENCY_CACHE/manifest.json" \
  --cache "$DEPENDENCY_CACHE" --artifact-id source-ecc --destination "$VENDOR" \
  --expected-version "$ECC_COMMIT" --expected-requested-version "$ECC_PIN" >/dev/null
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" validate-source \
  --root "$ROOT" --manifest "$DEPENDENCY_CACHE/manifest.json" \
  --cache "$DEPENDENCY_CACHE" --artifact-id source-ecc --destination "$VENDOR" \
  --expected-version "$ECC_COMMIT" --expected-requested-version "$ECC_PIN" >/dev/null
echo "==> ECC policy-bound vendor tree installed (${ECC_COMMIT})"

# Catalog every skill directory (any depth: <name>/SKILL.md) as "name<TAB>dir".
CATALOG="$(mktemp)"
trap 'rm -f "$CATALOG"' EXIT
find "$VENDOR" -name SKILL.md -not -path "*/node_modules/*" 2>/dev/null | while IFS= read -r skill_md; do
  dir="$(dirname "$skill_md")"
  printf '%s\t%s\n' "$(basename "$dir")" "$dir"
done > "$CATALOG"

echo "==> ECC catalog: $(wc -l < "$CATALOG" | xargs) skills available"

lookup() {  # lookup <name> -> dir (empty if absent)
  awk -F'\t' -v n="$1" '$1 == n { print $2; exit }' "$CATALOG"
}

install_one() {  # install_one <name> <dir>
  rm -rf "$CC_DIR/ecc-$1" "$OC_DIR/ecc-$1"
  cp -R "$2" "$CC_DIR/ecc-$1"
  cp -R "$2" "$OC_DIR/ecc-$1"
  echo "    + ecc-$1"
}

installed=0; missing=""
while IFS= read -r line; do
  line="$(echo "$line" | xargs)"
  [[ -z "$line" || "$line" == \#* ]] && continue
  if [[ "$line" == ~* ]]; then
    pat="${line#\~}"; hit=0
    while IFS=$'\t' read -r name dir; do
      if [[ "$name" == *"$pat"* ]]; then
        install_one "$name" "$dir"; installed=$((installed+1)); hit=1
      fi
    done < "$CATALOG"
    [[ $hit -eq 0 ]] && missing="$missing ~$pat"
  else
    dir="$(lookup "$line")"
    if [[ -n "$dir" ]]; then
      install_one "$line" "$dir"; installed=$((installed+1))
    else
      missing="$missing $line"
    fi
  fi
done < "$PROFILE"

echo "==> installed $installed ECC skills (prefixed 'ecc-') into both engines"
if [[ -n "$missing" ]]; then
  echo "NOTE: no catalog match for:$missing"
  echo "      Full catalog of available names:"
  cut -f1 "$CATALOG" | sort | sed 's/^/        /'
  echo "      Tune harness/ecc/profile.txt and re-run 'make ecc' if desired."
fi
