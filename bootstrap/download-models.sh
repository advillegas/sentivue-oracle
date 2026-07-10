#!/usr/bin/env bash
# Download the model ensemble from Hugging Face into models/. Resumable; re-run freely.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p models

HF="hf"
command -v hf >/dev/null || HF="$HOME/.local/bin/hf"
command -v "$HF" >/dev/null || { echo "ERROR: hf CLI missing — run 'make install'"; exit 1; }

df -g . | awk 'NR==2 { if ($4 < 800) print "NOTE: "$4" GB free — the FULL ensemble needs ~700 GB + headroom" }'

PROFILE="serving/models.profile"
is_active() { [[ ! -f "$PROFILE" ]] || grep -qx "$1" "$PROFILE"; }

grep -Ev '^\s*(#|$)' serving/models.manifest | while IFS='|' read -r name repo include slot ctx flags; do
  name="$(echo "$name" | xargs)"; repo="$(echo "$repo" | xargs)"; include="$(echo "$include" | xargs)"
  is_active "$name" || { echo "==> (profile) skipping $name"; continue; }
  echo "==> $name  ($repo :: $include)"
  "$HF" download "$repo" --include "$include" --local-dir "models/$name"
done

echo
echo "All downloads complete:"
du -sh models/* 2>/dev/null || true
echo "Next: make render && make serve"
