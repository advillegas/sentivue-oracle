#!/usr/bin/env bash
# harden-egress.sh - DEFAULT-DENY outbound egress (macOS/Linux).
#
# On macOS the enforcement point is pf (packet filter), which blocks ALL
# outbound traffic except loopback. That is a strict superset of per-process
# rules: it covers every process class the appliance runs - the editor,
# extension hosts, agent engines, inference servers, agent-spawned package
# managers / MCP servers, and containers - without trusting any of them. Local
# services on 127.0.0.1 (llama-swap, llama-server, Supabase, the console) keep
# working; the public internet is denied.
#
#   sudo bash bootstrap/harden-egress.sh on       enable default-deny
#   sudo bash bootstrap/harden-egress.sh off       remove enforcement
#        bash bootstrap/harden-egress.sh status     show pf state + covered classes
#        bash bootstrap/harden-egress.sh plan        list covered process classes
#
# The envoy network window drops this, fetches, and restores it (see envoy.sh) -
# the same toggle model as the Windows firewall guard.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"

covered_classes() {
  cat <<'EOF'
  Covered process classes (pf blocks all non-loopback egress, so every process
  is included; these are the appliance's expected network-capable runtimes):
    - VSCodium (main, extension host, renderer)
    - agent engines: Claude Code, OpenCode, Kilo Code
    - inference servers: llama-swap, llama-server
    - agent-spawned package managers: npm/npx, pip, uv/uvx
    - MCP servers (node/python/uvx launched by agents)
    - containers launched by agents (Docker/Supabase stack)
  Loopback (127.0.0.1, ::1) is exempt, so local model serving keeps working.
EOF
}

case "$ACTION" in
  on)   exec sudo bash "$ROOT/bootstrap/harden-offline.sh" on ;;
  off)  exec sudo bash "$ROOT/bootstrap/harden-offline.sh" off ;;
  plan) echo "== egress default-deny plan (macOS: global pf, loopback exempt) =="; covered_classes ;;
  status)
    if sudo -n pfctl -sr 2>/dev/null | grep -q "block .*out" || pfctl -sr 2>/dev/null | grep -q "block .*out"; then
      echo "egress default-deny ACTIVE (pf block-out anchor loaded)."
    else
      echo "egress default-deny INACTIVE. Enable: oracle harden"
    fi
    echo; covered_classes ;;
  *) echo "usage: bootstrap/harden-egress.sh {on|off|status|plan}"; exit 1 ;;
esac
