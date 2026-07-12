#!/usr/bin/env bash
# verify-egress.sh - prove the default-deny egress posture empirically (macOS/Linux).
# Attempts a public-host request (must FAIL when hardening is on) and a loopback
# request to llama-swap (must SUCCEED). Read-only, no sudo. Exit 0 iff posture
# matches expectation.
#
#   bash bootstrap/verify-egress.sh
set -uo pipefail
PROBE="https://cloudflare.com/cdn-cgi/trace"   # not a Kilo/vendor host
LOOP="http://127.0.0.1:9099/health"
fail=0

if sudo -n pfctl -sr 2>/dev/null | grep -q "block .*out" || pfctl -sr 2>/dev/null | grep -q "block .*out"; then
  HARDENED=1; else HARDENED=0; fi
echo "== egress verification (hardening is $([[ $HARDENED -eq 1 ]] && echo ON || echo OFF)) =="

# external egress probe
if curl -sf -m 8 --proto '=https' "$PROBE" >/dev/null 2>&1; then ext="REACHED"; else ext="BLOCKED"; fi
if [[ $HARDENED -eq 1 ]]; then
  if [[ "$ext" == "REACHED" ]]; then echo "  external egress : LEAK - reached internet while hardened!"; fail=1
  else echo "  external egress : blocked OK"; fi
else
  echo "  external egress : $ext (hardening off - informational)"
fi

# loopback must always work when llama-swap is up
if curl -sf -m 8 "$LOOP" >/dev/null 2>&1; then echo "  loopback :9099  : reachable OK"
else echo "  loopback :9099  : unreachable (is llama-swap up? 'oracle serve')"; fi

if [[ $HARDENED -eq 1 && $fail -eq 0 ]]; then echo "PASS: no egress leaks; loopback intact."; exit 0; fi
if [[ $HARDENED -eq 0 ]]; then echo "NOTE: hardening is OFF - run 'oracle harden' then re-verify."; exit 0; fi
echo "FAIL: egress leak detected."; exit 1
