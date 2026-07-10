#!/usr/bin/env bash
# Local git vault — a private, offline "origin" for the air-gapped Mac.
#
# Plain bare repositories under $ORACLE_VAULT (default ~/oracle-git-vault),
# addressed by file:// paths. Zero dependencies beyond git itself. The vault
# repos are history-protected (no deletes, no non-fast-forward) so they act as
# an append-only backup, not a working copy.
#
#   oracle vault init              create the vault + register the 'vault' remote
#                                  on this repo + first full push
#   oracle vault sync [repo]       push --all --tags from a repo (default: here);
#                                  auto-creates the bare repo on first sync
#   oracle vault new <name>        create an empty bare repo for a new project
#   oracle vault clone <name> [to] clone a project out of the vault
#   oracle vault list              repos, branches, last activity, sizes
#   oracle vault backup [destdir]  tar the whole vault (USB/external rotation)
#
# The conductor pushes mission branches here automatically after every merge
# (silently, if the 'vault' remote exists) and everything at mission end.
#
# To intentionally rewrite history in a vault repo (rare):
#   git -C "$ORACLE_VAULT/<name>.git" config receive.denyNonFastForwards false
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="${ORACLE_VAULT:-$HOME/oracle-git-vault}"

bare_init() {  # bare_init <name>
  local path="$VAULT/$1.git"
  if [[ ! -d "$path" ]]; then
    git init --bare --initial-branch=main --quiet "$path"
    git -C "$path" config receive.denyDeletes true
    git -C "$path" config receive.denyNonFastForwards true
    echo "  created $path (history-protected)"
  fi
}

ensure_remote() {  # ensure_remote <repo-dir> <name>
  local repo="$1" name="$2"
  bare_init "$name"
  if git -C "$repo" remote get-url vault >/dev/null 2>&1; then
    git -C "$repo" remote set-url vault "$VAULT/$name.git"
  else
    git -C "$repo" remote add vault "$VAULT/$name.git"
  fi
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  init)
    mkdir -p "$VAULT"
    ensure_remote "$ROOT" "sentivue-oracle"
    git -C "$ROOT" push --quiet vault --all
    git -C "$ROOT" push --quiet vault --tags
    echo "vault ready: $VAULT"
    echo "  remote 'vault' registered on $ROOT; all branches + tags pushed"
    ;;
  sync)
    repo="${1:-$ROOT}"
    [[ -d "$repo/.git" ]] || { echo "ERROR: $repo is not a git repo"; exit 1; }
    mkdir -p "$VAULT"
    ensure_remote "$repo" "$(basename "$repo")"
    git -C "$repo" push vault --all
    git -C "$repo" push --quiet vault --tags
    echo "synced $(basename "$repo") -> vault"
    ;;
  new)
    name="${1:?usage: oracle vault new <name>}"
    mkdir -p "$VAULT"
    bare_init "$name"
    echo "clone with: git clone \"$VAULT/$name.git\""
    ;;
  clone)
    name="${1:?usage: oracle vault clone <name> [dest]}"
    git clone "$VAULT/$name.git" "${2:-$name}"
    ;;
  list)
    [[ -d "$VAULT" ]] || { echo "no vault yet — run: oracle vault init"; exit 0; }
    for r in "$VAULT"/*.git; do
      [[ -d "$r" ]] || continue
      name="$(basename "$r" .git)"
      last="$(git -C "$r" for-each-ref --count=1 --sort=-committerdate \
              --format='%(committerdate:short) %(refname:short)' refs/heads 2>/dev/null)"
      nbr="$(git -C "$r" for-each-ref refs/heads | wc -l | xargs)"
      size="$(du -sh "$r" 2>/dev/null | cut -f1)"
      printf "  %-24s %3s branches  %8s  last: %s\n" "$name" "$nbr" "$size" "${last:-empty}"
    done
    ;;
  backup)
    dest="${1:-$ROOT/backups}"
    [[ -d "$VAULT" ]] || { echo "no vault to back up"; exit 1; }
    mkdir -p "$dest"
    out="$dest/vault-$(date +%Y%m%d-%H%M).tar.gz"
    tar -czf "$out" -C "$(dirname "$VAULT")" "$(basename "$VAULT")"
    echo "vault backup: $out ($(du -sh "$out" | cut -f1)) — rotate to external media"
    ;;
  help|*)
    sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
esac
