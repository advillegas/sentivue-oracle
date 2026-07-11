#!/usr/bin/env bash
# setup-agent-mcp.sh - Agent-MCP orchestration viewer (OPTIONAL component, mac twin).
# Multi-agent coordination server + live dashboard bound to 127.0.0.1, pointed
# at llama-swap (embeddings via the text-embedding-3-large alias).
#
#   bash harness/agent-mcp/setup-agent-mcp.sh install | start | stop | status
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENDOR="$ROOT/harness/agent-mcp/vendor"
source "$ROOT/VERSIONS.lock"
PORT=8100
DASH_PORT=3847
SRV_PID="$ROOT/state/agent-mcp.pid"
DASH_PID="$ROOT/state/agent-mcp-dash.pid"

local_env() {
  export OPENAI_API_KEY="oracle-local"
  export OPENAI_BASE_URL="http://127.0.0.1:9099/v1"
  export AGENT_MCP_HOST="127.0.0.1"
  export AGENT_MCP_PORT="$PORT"
  export AGENT_MCP_PROJECT_DIR="$ROOT"
  export NEXT_TELEMETRY_DISABLED=1
}

case "${1:-status}" in
  install)
    command -v uv >/dev/null || { echo "ERROR: uv missing - run bootstrap/ensure-tools.sh"; exit 1; }
    if [[ ! -d "$VENDOR/.git" ]]; then
      echo "==> cloning Agent-MCP ${AGENT_MCP_PIN} (shallow, pinned)"
      git clone --depth 1 --branch "$AGENT_MCP_PIN" "$AGENT_MCP_REPO" "$VENDOR"
    else
      echo "==> Agent-MCP vendor checkout present"
    fi
    ( cd "$VENDOR" && (uv sync 2>/dev/null || { uv venv; uv pip install -e .; }) )
    # upstream uses Starlette's on_startup kwarg (removed in 0.47) but pins loosely
    ( cd "$VENDOR" && uv pip install "starlette<0.47" )
    if [[ -f "$VENDOR/agent_mcp/dashboard/package.json" ]]; then
      echo "==> dashboard deps (npm install)"
      ( cd "$VENDOR/agent_mcp/dashboard" && npm install --no-audit --no-fund >/dev/null )
    fi
    echo "==> Agent-MCP installed. Start the viewer with: oracle agents-ui"
    ;;
  start)
    [[ -d "$VENDOR" ]] || { echo "not installed - run: bash harness/agent-mcp/setup-agent-mcp.sh install"; exit 1; }
    if [[ -f "$ROOT/state/conductor.lock" ]]; then
      echo "WARNING: a mission is running (state/conductor.lock). On shared-CPU hardware"
      echo "         the viewer's model calls compete with engine inference."
    fi
    mkdir -p "$ROOT/state" "$ROOT/logs"
    local_env
    # --no-index: the auto-RAG indexer floods the local embedding slot with
    # multi-minute batches and starves engine inference on shared hardware.
    ( cd "$VENDOR" && nohup uv run --no-sync -m agent_mcp.cli --port "$PORT" --project-dir "$ROOT" --no-tui --no-index \
        > "$ROOT/logs/agent-mcp.out.log" 2> "$ROOT/logs/agent-mcp.err.log" & echo $! > "$SRV_PID" )
    if [[ -f "$VENDOR/agent_mcp/dashboard/package.json" ]]; then
      # bypass upstream's dev wrapper (binds 0.0.0.0); run next directly, loopback only
      ( cd "$VENDOR/agent_mcp/dashboard" && nohup npx next dev --port "$DASH_PORT" --hostname 127.0.0.1 \
          > "$ROOT/logs/agent-mcp-dash.out.log" 2> "$ROOT/logs/agent-mcp-dash.err.log" & echo $! > "$DASH_PID" )
    fi
    echo "Agent-MCP server:    http://127.0.0.1:$PORT  (MCP endpoint /mcp)"
    echo "Orchestration view:  http://127.0.0.1:$DASH_PORT  (give it ~20s to compile)"
    ;;
  stop)
    for f in "$SRV_PID" "$DASH_PID"; do
      if [[ -f "$f" ]]; then
        pkill -P "$(cat "$f")" 2>/dev/null || true
        kill "$(cat "$f")" 2>/dev/null || true
        rm -f "$f"
      fi
    done
    echo "agent-mcp stopped"
    ;;
  status)
    # TCP-level probes: the server has no stable health path across versions
    (echo > "/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1 \
      && echo "server: UP (http://127.0.0.1:$PORT)" || echo "server: DOWN"
    (echo > "/dev/tcp/127.0.0.1/$DASH_PORT") >/dev/null 2>&1 \
      && echo "viewer: UP (http://127.0.0.1:$DASH_PORT)" || echo "viewer: DOWN"
    ;;
  *) echo "usage: setup-agent-mcp.sh {install|start|stop|status}" ;;
esac
