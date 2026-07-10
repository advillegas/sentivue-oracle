#!/usr/bin/env bash
# oracle envoy — open a controlled network window and run the envoy agent.
#
#   oracle envoy            interactive envoy session (Claude Code engine)
#   oracle envoy --queue    headless: process memory/NET-REQUESTS.md and exit
#   oracle envoy --engine opencode
#
# Sequence: warn -> drop the pf air-gap if it is active -> run the envoy with its
# restricted permission set -> restore the air-gap on exit (trap, even on crash).
# Workers keep running fully offline the whole time: their engines deny all
# network tools regardless of the firewall state.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENGINE="claude"; QUEUE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE=1 ;;
    --engine) ENGINE="$2"; shift ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac; shift
done

mkdir -p incoming memory
[[ -f memory/NET-REQUESTS.md ]] || cat > memory/NET-REQUESTS.md <<'EOF'
# Network request queue — workers append needs; the envoy fulfils them.
# Format:
#   - [ ] <date> <mission>/<task>: NEED <pip:pkg==ver | npm:pkg@ver | url> — WHY <...> — USED-IN <path>
EOF

WAS_HARDENED=0
if sudo -n pfctl -sr 2>/dev/null | grep -q "block drop out" \
   || sudo pfctl -sr 2>/dev/null | grep -q "block drop out"; then
  WAS_HARDENED=1
fi

restore() {
  if [[ $WAS_HARDENED -eq 1 ]]; then
    echo "==> network window closing: restoring the air-gap"
    sudo bash "$ROOT/bootstrap/harden-offline.sh" || \
      echo "WARN: re-harden failed — run 'oracle harden' manually NOW"
  fi
}
trap restore EXIT

echo "=============================================================="
echo " ENVOY NETWORK WINDOW"
echo "  - only the envoy runs online; workers remain network-denied"
echo "  - fetch-only: envoy-fetch, allowlisted domains, quarantine"
echo "  - the air-gap is restored automatically when this session ends"
echo "=============================================================="
if [[ $WAS_HARDENED -eq 1 ]]; then
  sudo bash "$ROOT/bootstrap/harden-offline.sh" off
fi

export PATH="$ROOT/bin:$PATH"
DOCTRINE="$(cat "$ROOT/engines/shared/ENVOY.md")"

if [[ "$ENGINE" == "claude" ]]; then
  ARGS=(--settings "$ROOT/engines/claude-code/envoy-settings.json"
        --append-system-prompt "$DOCTRINE")
  if [[ $QUEUE -eq 1 ]]; then
    bash engines/claude-code/launch.sh "${ARGS[@]}" \
      -p "Process every open item in memory/NET-REQUESTS.md per your envoy doctrine, then summarize what was fetched, refused, or needs specification." \
      --output-format text
  else
    bash engines/claude-code/launch.sh "${ARGS[@]}"
  fi
elif [[ "$ENGINE" == "opencode" ]]; then
  if [[ $QUEUE -eq 1 ]]; then
    bash engines/opencode/launch.sh run --agent envoy \
      "Process every open item in memory/NET-REQUESTS.md per your envoy doctrine, then summarize what was fetched, refused, or needs specification."
  else
    bash engines/opencode/launch.sh --agent envoy
  fi
else
  echo "unknown engine: $ENGINE"; exit 1
fi
