#!/usr/bin/env bash
# Download the pinned model ensemble and promote only checksum-matching snapshots.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p models
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"

HF="hf"
command -v hf >/dev/null || HF="$HOME/.local/bin/hf"
command -v "$HF" >/dev/null || { echo "ERROR: hf CLI missing — run 'make install'"; exit 1; }
PYTHON_BIN="$ROOT/env/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "ERROR: Python is required for model provenance."; exit 1; }

df -g . | awk 'NR==2 { if ($4 < 1100) print "NOTE: "$4" GB free — the FULL profile needs ~1.0 TB + headroom" }'

PROFILE="serving/models.profile"
is_active() { [[ ! -f "$PROFILE" ]] || grep -qx "$1" "$PROFILE"; }
AUTHORITY_COPY="$(mktemp "${TMPDIR:-/tmp}/oracle-model-authorities.XXXXXX.json")"
trap 'rm -f "$AUTHORITY_COPY"' EXIT
cp serving/model-authorities.json "$AUTHORITY_COPY"

grep -Ev '^\s*(#|$)' serving/models.manifest |
while IFS='|' read -r name repo include slot ctx flags revision; do
  name="$(echo "$name" | xargs)"; repo="$(echo "$repo" | xargs)"; include="$(echo "$include" | xargs)"
  revision="$(echo "$revision" | xargs)"
  is_active "$name" || { echo "==> (profile) skipping $name"; continue; }
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] ||
    { echo "ERROR: model $name lacks a pinned revision" >&2; exit 1; }
  echo "==> $name  ($repo@$revision :: $include)"
  "$HF" download "$repo" --revision "$revision" \
    --include "$include" --local-dir "models/$name"
  "$PYTHON_BIN" verification/lifecycle.py import-model \
    --root "$ROOT" --cache "$DEPENDENCY_CACHE" --models-root "$ROOT/models" \
    --model-name "$name" --authority "$AUTHORITY_COPY" >/dev/null
  echo "    verified every shard against promoted SHA-256 policy"
done

echo
echo "Policy-bound model acquisition complete:"
du -sh models/* 2>/dev/null || true
