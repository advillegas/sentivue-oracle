#!/usr/bin/env bash
# security-audit.sh - full platform privacy/security sweep (macOS/Linux twin).
# Deterministic checks over the repo's privacy invariants; prints PASS/WARN/FAIL
# and exits nonzero on any FAIL. Read-only.
#
#   bash bootstrap/security-audit.sh [--deep]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEEP=0; [[ "${1:-}" == "--deep" ]] && DEEP=1
pass=0; warn=0; bad=0
ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
warnf(){ echo "  [WARN] $1"; warn=$((warn+1)); }
badf() { echo "  [FAIL] $1"; bad=$((bad+1)); }
has()  { grep -Eq "$2" "$ROOT/$1" 2>/dev/null; }

echo "=== SentiVue Oracle security sweep =============================="
echo
echo "== service bind addresses (must be loopback) =="
has "serving/service.sh" '127\.0\.0\.1:9099' && ok "llama-swap listens on 127.0.0.1:9099" || badf "llama-swap listen not loopback"
has "bootstrap/render-config.sh" '127\.0\.0\.1|--host 127' && ok "llama-server host loopback (render-config)" || warnf "llama-server host not asserted in render-config"
if grep -Eq '^\s*-\s*"(?!127\.0\.0\.1)[0-9]' "$ROOT/connectors/supabase/docker-compose.yml" 2>/dev/null; then
  badf "supabase publishes a non-loopback port"; else ok "supabase ports all bound to 127.0.0.1"; fi
has "conductor/console.py" 'ThreadingHTTPServer\(\("127\.0\.0\.1"' && ok "console bound to 127.0.0.1" || badf "console bind not loopback"
has "harness/agent-mcp/setup-agent-mcp.sh" 'AGENT_MCP_HOST="127\.0\.0\.1"' && ok "agent-mcp host = 127.0.0.1" || warnf "agent-mcp host binding not asserted"

echo
echo "== engine telemetry / auto-update kill-switches =="
has "engines/claude-code/home/settings.json" "DISABLE_TELEMETRY" && ok "Claude Code telemetry disabled" || badf "Claude Code telemetry env missing"
has "engines/claude-code/home/settings.json" "DISABLE_AUTOUPDATER|DISABLE_NONESSENTIAL" && ok "Claude Code autoupdate/nonessential traffic off" || warnf "Claude Code autoupdate flag not found"
has "engines/opencode/xdg/opencode/opencode.json" '"webfetch":[[:space:]]*"deny"' && ok "OpenCode workers deny webfetch" || badf "OpenCode webfetch not denied"
has "connectors/ide/setup-ide.sh" 'telemetryLevel.*off|"off"' && ok "VSCodium telemetry off at setup" || warnf "VSCodium telemetry not asserted"
has "connectors/ide/setup-ide.sh" 'update.mode.*none' && ok "VSCodium auto-update disabled" || warnf "VSCodium update.mode not pinned"

echo
echo "== Kilo hardening layer =="
for f in engines/kilo/hardened-env.ps1 engines/kilo/hardened-env.sh engines/kilo/call-home-hosts.txt engines/kilo/HARDENING.md; do
  [[ -f "$ROOT/$f" ]] && ok "present: $f" || badf "missing: $f"
done
has "engines/kilo/launch.sh" "hardened-env.sh" && ok "launch.sh sources the hardening profile" || badf "launch.sh does not source hardened-env"
has "engines/kilo/launch.ps1" "hardened-env.ps1" && ok "launch.ps1 sources the hardening profile" || badf "launch.ps1 does not source hardened-env"
for k in KILO_TELEMETRY_LEVEL KILO_DISABLE_SHARE KILO_DISABLE_AUTOUPDATE KILO_DISABLE_MODELS_FETCH OTEL_SDK_DISABLED KILO_DISABLE_SESSION_INGEST; do
  has "engines/kilo/hardened-env.sh" "$k" && ok "defang: $k" || badf "defang missing: $k"
done
has "connectors/ide/sync-models.sh" '"\$schema":[[:space:]]*"https://app\.kilo\.ai' && badf "generated kilo.jsonc still sets remote schema (sh)" || ok "no remote schema key in generated kilo.jsonc (sh)"
kilo_cfg="$ROOT/state/generated/kilo/kilo.jsonc"
if [[ -f "$kilo_cfg" ]]; then
  grep -q 'app\.kilo\.ai' "$kilo_cfg" && badf "generated kilo.jsonc references app.kilo.ai (re-run sync-models)" || ok "generated kilo.jsonc has no kilo.ai references"
fi

echo
echo "== egress default-deny guard =="
for f in bootstrap/harden-egress.ps1 bootstrap/harden-egress.sh bootstrap/verify-egress.ps1 bootstrap/verify-egress.sh; do
  [[ -f "$ROOT/$f" ]] && ok "present: $f" || badf "missing: $f"
done
if sudo -n pfctl -sr 2>/dev/null | grep -q "block .*out" || pfctl -sr 2>/dev/null | grep -q "block .*out"; then
  ok "egress default-deny ACTIVE"; else warnf "egress default-deny INACTIVE (opt-in: 'oracle harden')"; fi

echo
echo "== secret hygiene =="
for p in '\.env$' '\.env\.' 'credentials' '\*\.key' 'id_rsa'; do
  has ".gitignore" "$p" && ok ".gitignore covers /$p/" || warnf ".gitignore missing /$p/"
done
leak=0
while IFS= read -r f; do
  case "$f" in *.md|*.lock|*.txt) continue;; esac
  if grep -Eq 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}' "$ROOT/$f" 2>/dev/null; then
    badf "possible secret in tracked file: $f"; leak=$((leak+1)); fi
done < <(git -C "$ROOT" ls-files 2>/dev/null)
[[ $leak -eq 0 ]] && ok "no obvious secrets in tracked files"

if [[ $DEEP -eq 1 ]]; then
  echo
  echo "== deep: vendored Kilo binary endpoint scan =="
  kbin="$ROOT/.tools/npm/node_modules/@kilocode/cli/bin/.kilo"
  if [[ -f "$kbin" ]]; then
    node "$ROOT/bootstrap/scan-binary.mjs" "$kbin" --hosts-only >/dev/null 2>&1 && ok "deep scan completed" || warnf "deep scan could not run (node?)"
  else warnf "kilo binary not present to scan"; fi
fi

echo
echo "=== sweep result: $pass PASS / $warn WARN / $bad FAIL ==="
if [[ $bad -gt 0 ]]; then echo "SECURITY SWEEP FAILED - fix the [FAIL] items above."; exit 1; fi
echo "Security invariants hold. (WARNs are advisory / opt-in toggles.)"
exit 0
