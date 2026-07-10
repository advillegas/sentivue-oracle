#!/usr/bin/env bash
# Sync skills/ (source of truth) into both engines via symlinks:
#   Claude Code: engines/claude-code/home/skills/<name>   (CLAUDE_CONFIG_DIR)
#   OpenCode:    engines/opencode/xdg/opencode/skill/<name>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_DIR="$ROOT/engines/claude-code/home/skills"
OC_DIR="$ROOT/engines/opencode/xdg/opencode/skill"
mkdir -p "$CC_DIR" "$OC_DIR"

count=0
for d in "$ROOT"/skills/*/; do
  [[ -f "$d/SKILL.md" ]] || continue
  name="$(basename "$d")"
  ln -sfn "${d%/}" "$CC_DIR/$name"
  ln -sfn "${d%/}" "$OC_DIR/$name"
  count=$((count+1))
done
echo "Synced $count skills into both engines."
