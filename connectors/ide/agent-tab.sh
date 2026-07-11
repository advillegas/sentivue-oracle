#!/usr/bin/env bash
# agent-tab.sh - one agent per terminal tab, Cursor-style (macOS twin).
# Opened by the IDE terminal profiles ("Oracle Agent: ...") or a keybinding;
# open as many tabs as you want - each is an independent engine session on the
# local models. --worktree gives the agent its own git worktree so parallel
# agents never collide on files (merge back when you like the result).
#
#   agent-tab.sh claude               agent in the repo itself
#   agent-tab.sh claude --worktree    agent in an isolated worktree + branch
#   agent-tab.sh opencode
#   agent-tab.sh kilo
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENGINE="${1:-claude}"
WT="${2:-}"

DIR="$ROOT"
BRANCH=""
if [[ "$WT" == "--worktree" ]]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  BRANCH="agent/tab-$STAMP"
  DIR="$ROOT/.worktrees/tab-$STAMP"
  git -C "$ROOT" worktree add -b "$BRANCH" "$DIR" >/dev/null
fi

NAME="Claude Code"; LAUNCH="engines/claude-code/launch.sh"
if [[ "$ENGINE" == "opencode" ]]; then NAME="OpenCode"; LAUNCH="engines/opencode/launch.sh"; fi
if [[ "$ENGINE" == "kilo" ]]; then NAME="Kilo Code"; LAUNCH="engines/kilo/launch.sh"; fi

echo ""
echo "  SentiVue Oracle agent tab - $NAME (local models)"
if [[ -n "$BRANCH" ]]; then
  echo "  isolated worktree: $DIR"
  echo "  branch: $BRANCH  (merge back with: git merge $BRANCH)"
else
  echo "  working directly in: $DIR  (open a worktree tab for parallel isolation)"
fi
echo ""

cd "$DIR"
bash "$ROOT/$LAUNCH" || true

echo ""
if [[ -n "$BRANCH" ]]; then
  echo "  agent exited. keep:   git merge $BRANCH"
  echo "          discard:  git worktree remove $DIR && git branch -D $BRANCH"
else
  echo "  agent exited - dropping to a shell here (rerun with the up arrow)"
fi
exec "${SHELL:-/bin/zsh}" -i
