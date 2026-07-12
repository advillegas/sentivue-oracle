#!/usr/bin/env bash
# Launch the Claude Code engine, fully self-contained in this repo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export CLAUDE_CONFIG_DIR="$ROOT/engines/claude-code/home"
export ORACLE_ROOT="$ROOT"
export UV_OFFLINE=1
export UV_CACHE_DIR="$ROOT/incoming/dependency-cache/uv"
export ORACLE_PG_PASSWORD="${ORACLE_PG_PASSWORD:-$(grep -s '^POSTGRES_PASSWORD=' "$ROOT/connectors/supabase/.env" | cut -d= -f2 || true)}"
export PATH="$ROOT/.tools/bin:$ROOT/.tools/npm/bin:$PATH"
GENERATED_SETTINGS="$ROOT/state/generated/claude-code/settings.json"

command -v claude >/dev/null || { echo "ERROR: claude not installed — run 'make install'"; exit 1; }
bash "$ROOT/connectors/ide/sync-models.sh" >/dev/null
[[ -f "$GENERATED_SETTINGS" ]] || {
  echo "ERROR: generated Claude settings are unavailable; install a validated model snapshot" >&2
  exit 1
}
curl -sf -m 5 "http://127.0.0.1:9099/health" >/dev/null 2>&1 \
  || echo "WARN: llama-swap not responding — run 'make serve'" >&2

exec claude --settings "$GENERATED_SETTINGS" \
  --mcp-config "$ROOT/connectors/mcp.claude.json" "$@"
