#!/usr/bin/env bash
# Launch the OpenCode engine, fully self-contained in this repo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export XDG_CONFIG_HOME="$ROOT/engines/opencode/xdg"
export XDG_DATA_HOME="$ROOT/engines/opencode/xdg-data"
export ORACLE_ROOT="$ROOT"
export UV_OFFLINE=1
export UV_CACHE_DIR="$ROOT/incoming/dependency-cache/uv"
export ORACLE_PG_PASSWORD="${ORACLE_PG_PASSWORD:-$(grep -s '^POSTGRES_PASSWORD=' "$ROOT/connectors/supabase/.env" | cut -d= -f2 || true)}"
export PATH="$ROOT/.tools/bin:$ROOT/.tools/npm/bin:$PATH"
export OPENCODE_CONFIG="$ROOT/state/generated/opencode/opencode.json"

command -v opencode >/dev/null || { echo "ERROR: opencode not installed — run 'make install'"; exit 1; }
bash "$ROOT/connectors/ide/sync-models.sh" >/dev/null
[[ -f "$OPENCODE_CONFIG" ]] || {
  echo "ERROR: generated OpenCode config is unavailable; install a validated model snapshot" >&2
  exit 1
}
curl -sf -m 5 "http://127.0.0.1:9099/health" >/dev/null 2>&1 \
  || echo "WARN: llama-swap not responding — run 'make serve'" >&2

exec opencode "$@"
