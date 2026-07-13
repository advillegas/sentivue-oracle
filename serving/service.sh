#!/usr/bin/env bash
# service.sh - macOS launchd twin for policy-bound local serving.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.sentivue.oracle-serving"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GENERATED="$ROOT/state/generated/serving"
GENERATED_PLIST="$GENERATED/$LABEL.plist"
CONFIG="$GENERATED/llama-swap.yaml"
ADMISSION="$GENERATED/admission.json"
PID_RECORD="$GENERATED/service.pid.json"
SERVER="$ROOT/.tools/bin/llama-server"
SWAP="$ROOT/.tools/bin/llama-swap"
SERVING="$ROOT/verification/serving.py"
LIFECYCLE="$ROOT/verification/lifecycle.py"

find_python() {
  local candidate
  for candidate in "$ROOT/env/.venv/bin/python" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
         >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || {
  echo "ERROR: Python 3.12 or newer is required for shared serving validation." >&2
  exit 127
}

render() {
  [[ -x "$SERVER" ]] || {
    echo "ERROR: policy-bound llama-server is missing: $SERVER" >&2
    return 1
  }
  local backend="${ORACLE_BACKEND:-cpu}"
  [[ "$backend" == "metal" || "$backend" == "cpu" ]] || {
    echo "ERROR: the macOS toolchain supports explicit metal or cpu only." >&2
    return 1
  }
  "$PYTHON" "$SERVING" render --root "$ROOT" --server "$SERVER" \
    --llama-swap "$SWAP" --platform posix --backend "$backend"
}

sync_engine_configs() {
  ORACLE_ROOT="$ROOT" ORACLE_HOME="$HOME" \
    bash "$ROOT/connectors/ide/sync-models.sh"
}

write_generated_plist() {
  mkdir -p "$GENERATED" "$ROOT/logs"
  "$PYTHON" "$SERVING" launchd-plist \
    --output "$GENERATED_PLIST" --label "$LABEL" --python "$PYTHON" \
    --root "$ROOT" --llama-swap "$SWAP" --config "$CONFIG" \
    --admission "$ADMISSION" --stdout "$ROOT/logs/serving.launchd.out.log" \
    --stderr "$ROOT/logs/serving.launchd.err.log" >/dev/null
}

verify_owned_launchd() {
  [[ -f "$PLIST" ]] || return 1
  [[ -f "$GENERATED_PLIST" ]] || {
    echo "ERROR: refusing launchd operation: Oracle ownership descriptor is missing." >&2
    return 1
  }
  cmp -s "$GENERATED_PLIST" "$PLIST" || {
    echo "ERROR: refusing launchd operation: $PLIST is not the Oracle descriptor." >&2
    return 1
  }
}

publish_plist() {
  mkdir -p "$(dirname "$PLIST")"
  local temporary
  temporary="$(mktemp "${PLIST}.tmp.XXXXXX")"
  trap 'rm -f "$temporary"' RETURN
  cp "$GENERATED_PLIST" "$temporary"
  mv -f "$temporary" "$PLIST"
  trap - RETURN
}

install_service() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    echo "ERROR: durable service install is macOS launchd scoped." >&2
    return 1
  }
  [[ -x "$SWAP" ]] || {
    echo "ERROR: policy-bound llama-swap is missing: $SWAP" >&2
    return 1
  }
  if [[ -f "$PLIST" ]]; then
    verify_owned_launchd
  fi
  render
  sync_engine_configs
  write_generated_plist
  publish_plist
  "$PYTHON" "$LIFECYCLE" state own --root "$ROOT" --home "$HOME" \
    --path "$PLIST" >/dev/null
  "$PYTHON" "$LIFECYCLE" state own-service --root "$ROOT" --home "$HOME" \
    --service-kind launchd-user --identifier "$LABEL" >/dev/null
  echo "installed launchd service: $LABEL"
}

stop_service() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if [[ -f "$PLIST" ]] ||
       launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      verify_owned_launchd
      launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    fi
  fi
  # The PID record is diagnostic evidence only. launchd owns termination; this
  # wrapper never kills an unverified or PID-reused process.
  if [[ -f "$PID_RECORD" ]]; then
    local pid
    pid="$("$PYTHON" -c \
      'import json,sys; print(int(json.load(open(sys.argv[1], encoding="utf-8"))["pid"]))' \
      "$PID_RECORD" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        echo "ERROR: owned launchd job stopped but PID $pid remains; refusing blind kill." >&2
        return 1
      fi
    fi
  fi
  echo "serving stopped"
}

start_service() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    echo "ERROR: durable service start is macOS launchd scoped." >&2
    return 1
  }
  if [[ ! -f "$PLIST" ]]; then
    install_service
  else
    verify_owned_launchd
    render
    sync_engine_configs
    write_generated_plist
    if ! cmp -s "$GENERATED_PLIST" "$PLIST"; then
      publish_plist
    fi
  fi
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "serving starting through launchd on 127.0.0.1:9099"
}

status_service() {
  local running=0
  if [[ "$(uname -s)" == "Darwin" ]] &&
     launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    running=1
  fi
  if [[ -f "$PLIST" || $running -eq 1 ]]; then
    verify_owned_launchd || return 1
  fi
  if [[ $running -eq 1 ]]; then
    echo "service: INSTALLED/RUNNING ($LABEL)"
  elif [[ -f "$PLIST" ]]; then
    echo "service: INSTALLED/STOPPED ($LABEL)"
  else
    echo "service: NOT INSTALLED"
  fi
  if curl -sf -m 3 "http://127.0.0.1:9099/health" >/dev/null 2>&1; then
    echo "gateway: HEALTHY (127.0.0.1:9099)"
  else
    echo "gateway: DOWN"
  fi
  [[ -f "$PID_RECORD" ]] && echo "pid evidence: $PID_RECORD" || true
}

case "${1:-status}" in
  render) render ;;
  install) install_service ;;
  uninstall)
    stop_service
    rm -f "$PLIST"
    echo "launchd service removed"
    ;;
  start) start_service ;;
  stop) stop_service ;;
  restart)
    stop_service
    start_service
    ;;
  status) status_service ;;
  capabilities)
    shift
    backend_args=()
    has_backend=0
    for value in "$@"; do
      [[ "$value" == "--backend" || "$value" == --backend=* ]] && has_backend=1
    done
    if [[ $has_backend -eq 0 ]]; then
      backend_args=(--backend "${ORACLE_BACKEND:-cpu}")
    fi
    exec "$PYTHON" "$SERVING" capabilities --root "$ROOT" \
      "${backend_args[@]}" "$@"
    ;;
  verify)
    shift
    exec "$PYTHON" "$SERVING" verify --root "$ROOT" "$@"
    ;;
  *)
    echo "usage: service.sh {render|install|uninstall|start|stop|restart|status|capabilities|verify}" >&2
    exit 2
    ;;
esac
