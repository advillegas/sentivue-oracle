#!/usr/bin/env bash
# Uninstall the appliance's system integration points. The repo itself stays.
#   oracle uninstall            remove services + PATH symlink (models kept)
#   oracle uninstall --purge    also delete models, tools, caches, vendor checkouts
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> stopping services"
bash serving/service.sh stop 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.sentivue.llamaswap" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.sentivue.llamaswap.plist"

if [[ -f /Library/LaunchDaemons/com.sentivue.wiredlimit.plist ]]; then
  echo "==> removing wired-limit LaunchDaemon (sudo)"
  sudo launchctl bootout system/com.sentivue.wiredlimit 2>/dev/null || true
  sudo rm -f /Library/LaunchDaemons/com.sentivue.wiredlimit.plist
fi

echo "==> removing pf hardening (if active)"
sudo bash bootstrap/harden-offline.sh off 2>/dev/null || true

echo "==> removing PATH symlink"
rm -f "$(brew --prefix 2>/dev/null)/bin/oracle" "$HOME/.local/bin/oracle" 2>/dev/null || true

if command -v docker >/dev/null 2>&1; then
  echo "==> stopping supabase stack (volumes kept unless --purge)"
  ( cd connectors/supabase && docker compose down 2>/dev/null ) || true
fi

if [[ "${1:-}" == "--purge" ]]; then
  echo "==> PURGE: models, tools, vendor, engine caches, runtime state"
  rm -rf models .tools harness/ecc/vendor engines/opencode/xdg-data \
         .worktrees logs state .install-state connectors/supabase/volumes
fi

echo "Uninstalled. The repo and your memory/ ledger remain at: $ROOT"
