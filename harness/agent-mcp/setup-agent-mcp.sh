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
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
ARTIFACT_MANIFEST="$DEPENDENCY_CACHE/manifest.json"
PYTHON_BIN="${ORACLE_PYTHON:-$ROOT/env/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required." >&2; exit 1; }
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
  export UV_OFFLINE=1
  export UV_CACHE_DIR="$DEPENDENCY_CACHE/uv"
}

validate_vendor() {
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" validate-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id source-agent-mcp --destination "$VENDOR" \
    --expected-version "$AGENT_MCP_COMMIT" \
    --expected-requested-version "$AGENT_MCP_PIN" >/dev/null
}

case "${1:-status}" in
  install)
    command -v uv >/dev/null || { echo "ERROR: uv missing - run bootstrap/ensure-tools.sh"; exit 1; }
    "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" install-source \
      --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
      --artifact-id source-agent-mcp --destination "$VENDOR" \
      --expected-version "$AGENT_MCP_COMMIT" \
      --expected-requested-version "$AGENT_MCP_PIN" >/dev/null
    validate_vendor
    local_env
    ( cd "$VENDOR" && uv sync --offline --frozen )
    if [[ -f "$VENDOR/agent_mcp/dashboard/package.json" ]]; then
      [[ -f "$VENDOR/agent_mcp/dashboard/package-lock.json" ]] ||
        { echo "ERROR: validated Agent-MCP export has no dashboard lock."; exit 1; }
      echo "==> dashboard deps (offline lock install)"
      ( cd "$VENDOR/agent_mcp/dashboard" &&
        npm ci --offline --ignore-scripts --no-audit --no-fund >/dev/null )
    fi
    echo "==> Agent-MCP installed. Start the viewer with: oracle agents-ui"
    ;;
  start)
    validate_vendor || { echo "not installed from a validated export"; exit 1; }
    if [[ -f "$ROOT/state/conductor.lock" ]]; then
      echo "WARNING: a mission is running (state/conductor.lock). On shared-CPU hardware"
      echo "         the viewer's model calls compete with engine inference."
    fi
    mkdir -p "$ROOT/state" "$ROOT/logs"
    local_env
    # --no-index: the auto-RAG indexer floods the local embedding slot with
    # multi-minute batches and starves engine inference on shared hardware.
    ( cd "$VENDOR" && nohup uv run --offline --no-sync -m agent_mcp.cli --port "$PORT" --project-dir "$ROOT" --no-tui --no-index \
        > "$ROOT/logs/agent-mcp.out.log" 2> "$ROOT/logs/agent-mcp.err.log" & echo $! > "$SRV_PID" )
    if [[ -f "$VENDOR/agent_mcp/dashboard/package.json" ]]; then
      next_bin="$VENDOR/agent_mcp/dashboard/node_modules/.bin/next"
      [[ -x "$next_bin" ]] || { echo "ERROR: offline dashboard runtime is missing"; exit 1; }
      ( cd "$VENDOR/agent_mcp/dashboard" && nohup "$next_bin" dev --port "$DASH_PORT" --hostname 127.0.0.1 \
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
