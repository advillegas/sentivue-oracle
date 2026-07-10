#!/usr/bin/env bash
# Launch the Claude Code engine, fully self-contained in this repo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export CLAUDE_CONFIG_DIR="$ROOT/engines/claude-code/home"
export ORACLE_ROOT="$ROOT"
export ORACLE_PG_PASSWORD="${ORACLE_PG_PASSWORD:-$(grep -s '^POSTGRES_PASSWORD=' "$ROOT/connectors/supabase/.env" | cut -d= -f2 || true)}"
export PATH="$ROOT/.tools/npm/bin:$PATH"

command -v claude >/dev/null || { echo "ERROR: claude not installed — run 'make install'"; exit 1; }
curl -sf -m 5 "http://127.0.0.1:9099/health" >/dev/null 2>&1 \
  || echo "WARN: llama-swap not responding — run 'make serve'" >&2

exec claude --mcp-config "$ROOT/connectors/mcp.claude.json" "$@"
