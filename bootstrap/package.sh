#!/usr/bin/env bash
# Package the repo into a clean tarball for transfer to the Mac Studio
# (USB/AirDrop-friendly — no cloud required). Runs on macOS, Linux, or
# Windows (Git Bash / tar.exe).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/.."
NAME="sentivue-oracle-$(date +%Y%m%d).tar.gz"

tar -czf "$NAME" \
  --exclude='sentivue-oracle/models' \
  --exclude='sentivue-oracle/.tools' \
  --exclude='sentivue-oracle/.worktrees' \
  --exclude='sentivue-oracle/logs' \
  --exclude='sentivue-oracle/state' \
  --exclude='sentivue-oracle/reports' \
  --exclude='sentivue-oracle/data' \
  --exclude='sentivue-oracle/artifacts' \
  --exclude='sentivue-oracle/harness/ecc/vendor' \
  --exclude='sentivue-oracle/engines/opencode/xdg-data' \
  --exclude='sentivue-oracle/connectors/supabase/volumes' \
  --exclude='sentivue-oracle/connectors/supabase/.env' \
  --exclude='sentivue-oracle/env/.venv' \
  --exclude='sentivue-oracle/.install-state' \
  --exclude='*.pyc' --exclude='__pycache__' --exclude='.DS_Store' \
  sentivue-oracle

echo "Wrote $(pwd)/$NAME"
echo "On the Mac:  tar -xzf $NAME && cd sentivue-oracle && bash install"
