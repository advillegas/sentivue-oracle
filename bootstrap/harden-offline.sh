#!/usr/bin/env bash
# OPTIONAL air-gap enforcement: pf rules that block ALL outbound traffic except
# loopback. Reversible. Run via `make harden` (needs sudo).
#
#   enable:  sudo bash bootstrap/harden-offline.sh
#   disable: sudo bash bootstrap/harden-offline.sh off
set -euo pipefail
ANCHOR="sentivue.oracle"
ANCHOR_FILE="/etc/pf.anchors/$ANCHOR"
MARKER="# sentivue-oracle-anchor"

if [[ "${1:-on}" == "off" ]]; then
  sudo sed -i '' "/$MARKER/d" /etc/pf.conf
  sudo pfctl -f /etc/pf.conf
  echo "Hardening removed. Outbound traffic restored."
  exit 0
fi

sudo tee "$ANCHOR_FILE" >/dev/null <<'EOF'
# SentiVue Oracle offline enforcement: allow loopback, kill everything outbound.
set skip on lo0
block out quick inet  all
block out quick inet6 all
EOF

if ! grep -q "$MARKER" /etc/pf.conf; then
  sudo tee -a /etc/pf.conf >/dev/null <<EOF
anchor "$ANCHOR" $MARKER
load anchor "$ANCHOR" from "$ANCHOR_FILE" $MARKER
EOF
fi

sudo pfctl -f /etc/pf.conf
sudo pfctl -E 2>/dev/null || true
echo "Offline enforcement ACTIVE: all outbound traffic blocked except loopback."
echo "Everything local (llama-swap, engines, Supabase on 127.0.0.1) keeps working."
echo "Undo with: sudo bash bootstrap/harden-offline.sh off"
