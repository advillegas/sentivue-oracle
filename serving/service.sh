#!/usr/bin/env bash
# llama-swap as a user launchd service (KeepAlive => service-level self-healing).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.sentivue.llamaswap"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONFIG="$ROOT/serving/llama-swap.rendered.yaml"
BIN="$ROOT/.tools/bin/llama-swap"
LOGDIR="$ROOT/logs"

write_plist() {
  mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$BIN</string>
    <string>--config</string>
    <string>$CONFIG</string>
    <string>--listen</string>
    <string>127.0.0.1:9099</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/llama-swap.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/llama-swap.err.log</string>
</dict></plist>
EOF
}

case "${1:-}" in
  start)
    [[ -f "$CONFIG" ]] || { echo "ERROR: $CONFIG missing — run 'make render' first"; exit 1; }
    [[ -x "$BIN"    ]] || { echo "ERROR: $BIN missing — run 'make install' first"; exit 1; }
    write_plist
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "llama-swap starting on http://127.0.0.1:9099 (logs: $LOGDIR)"
    ;;
  stop)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    echo "llama-swap stopped"
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    if curl -sf -m 5 "http://127.0.0.1:9099/health" >/dev/null 2>&1; then
      echo "llama-swap: HEALTHY"
      curl -sf -m 5 "http://127.0.0.1:9099/running" 2>/dev/null || true
      echo
    else
      echo "llama-swap: DOWN"
    fi
    echo "--- memory ledger ---"
    tail -n 5 "$ROOT/memory/LEDGER.md" 2>/dev/null || echo "(no ledger yet)"
    ;;
  *)
    echo "usage: service.sh {start|stop|restart|status}"; exit 1
    ;;
esac
