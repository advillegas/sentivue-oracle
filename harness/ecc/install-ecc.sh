#!/usr/bin/env bash
# Install a pinned, curated subset of ECC (harness skills) into both engines.
# ECC complements the engines: it is a config/skill layer, not an agent itself.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/VERSIONS.lock"
VENDOR="$ROOT/harness/ecc/vendor"
PROFILE="$ROOT/harness/ecc/profile.txt"
CC_DIR="$ROOT/engines/claude-code/home/skills"
OC_DIR="$ROOT/engines/opencode/xdg/opencode/skill"
mkdir -p "$CC_DIR" "$OC_DIR"

if [[ ! -d "$VENDOR/.git" ]]; then
  echo "==> cloning ECC ${ECC_PIN} (shallow, pinned)"
  git clone --depth 1 --branch "$ECC_PIN" "$ECC_REPO" "$VENDOR"
else
  echo "==> ECC vendor checkout present ($(git -C "$VENDOR" describe --tags --always))"
fi

# Catalog every skill directory in the checkout (any depth: <name>/SKILL.md).
declare -A CATALOG
while IFS= read -r skill_md; do
  dir="$(dirname "$skill_md")"
  CATALOG["$(basename "$dir")"]="$dir"
done < <(find "$VENDOR" -name SKILL.md -not -path "*/node_modules/*" 2>/dev/null)

echo "==> ECC catalog: ${#CATALOG[@]} skills available"

install_one() {
  local name="$1"
  rm -rf "$CC_DIR/ecc-$name" "$OC_DIR/ecc-$name"
  cp -R "${CATALOG[$name]}" "$CC_DIR/ecc-$name"
  cp -R "${CATALOG[$name]}" "$OC_DIR/ecc-$name"
  echo "    + ecc-$name"
}

installed=0; missing=()
while IFS= read -r line; do
  line="$(echo "$line" | xargs)"
  [[ -z "$line" || "$line" == \#* ]] && continue
  if [[ "$line" == ~* ]]; then
    pat="${line#\~}"; hit=0
    for name in "${!CATALOG[@]}"; do
      if [[ "$name" == *"$pat"* ]]; then
        install_one "$name"; installed=$((installed+1)); hit=1
      fi
    done
    [[ $hit -eq 0 ]] && missing+=("~$pat")
  elif [[ -n "${CATALOG[$line]:-}" ]]; then
    install_one "$line"; installed=$((installed+1))
  else
    missing+=("$line")
  fi
done < "$PROFILE"

echo "==> installed $installed ECC skills (prefixed 'ecc-') into both engines"
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "NOTE: no catalog match for: ${missing[*]}"
  echo "      Full catalog of available names:"
  printf '        %s\n' "${!CATALOG[@]}" | sort
  echo "      Tune harness/ecc/profile.txt and re-run 'make ecc' if desired."
fi
