#!/usr/bin/env bash
# install-skill-packs.sh - mac twin: vendor pinned superpowers + gstack and
# sync their skills into both engines (prefixes 'sp-' / 'gs-').
#   bash harness/skill-packs/install-skill-packs.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/harness/skill-packs"
source "$ROOT/VERSIONS.lock"
CC="$ROOT/engines/claude-code/home/skills"
OC="$ROOT/engines/opencode/xdg/opencode/skill"
mkdir -p "$CC" "$OC" "$HERE/vendor"

vendored() {  # vendored <name> <repo> <pin>
  local v="$HERE/vendor/$1"
  if [[ ! -d "$v/.git" ]]; then
    echo "==> cloning $1 @ $3 (pinned)" >&2
    if [[ "$3" =~ ^[0-9a-f]{40}$ ]]; then
      git clone --filter=blob:none "$2" "$v" >&2
      git -C "$v" checkout --quiet "$3"
    else
      git clone --depth 1 --branch "$3" "$2" "$v" >&2
    fi
  else
    echo "==> $1 vendor checkout present" >&2
  fi
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

SP="$(vendored superpowers "$SUPERPOWERS_REPO" "$SUPERPOWERS_PIN")"
echo "==> superpowers: $(sync_pack "$SP" sp skills) skills synced (prefix 'sp-')"
GS="$(vendored gstack "$GSTACK_REPO" "$GSTACK_PIN")"
echo "==> gstack: $(sync_pack "$GS" gs .) skills synced (prefix 'gs-')"
echo "Both packs live in harness/skill-packs/vendor; re-run after changing pins."
