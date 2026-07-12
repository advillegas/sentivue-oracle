#!/usr/bin/env bash
# Download the model ensemble from Hugging Face into models/. Resumable; re-run freely.
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

df -g . | awk 'NR==2 { if ($4 < 800) print "NOTE: "$4" GB free — the FULL ensemble needs ~700 GB + headroom" }'

PROFILE="serving/models.profile"
is_active() { [[ ! -f "$PROFILE" ]] || grep -qx "$1" "$PROFILE"; }

resolve_revision() {
  local repo="$1" requested="$2" resolved
  [[ "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    { echo "ERROR: unsafe model repository: $repo" >&2; return 1; }
  if [[ "$requested" == "dynamic" ]]; then
    resolved="$(curl -sf --proto '=https' "https://huggingface.co/api/models/$repo" |
      jq -r '.sha // empty')"
  else
    resolved="$requested"
  fi
  [[ "$resolved" =~ ^[0-9a-f]{40}$ ]] ||
    { echo "ERROR: model revision did not resolve to a commit: $repo" >&2; return 1; }
  printf '%s\n' "$resolved"
}

grep -Ev '^\s*(#|$)' serving/models.manifest |
while IFS='|' read -r name repo include slot ctx flags revision; do
  name="$(echo "$name" | xargs)"; repo="$(echo "$repo" | xargs)"; include="$(echo "$include" | xargs)"
  revision="$(echo "$revision" | xargs)"
  is_active "$name" || { echo "==> (profile) skipping $name"; continue; }
  resolved_revision="$(resolve_revision "$repo" "$revision")"
  echo "==> $name  ($repo@$resolved_revision :: $include)"
  "$HF" download "$repo" --revision "$resolved_revision" \
    --include "$include" --local-dir "models/$name"
  "$PYTHON_BIN" verification/lifecycle.py record-model \
    --root "$ROOT" --cache "$DEPENDENCY_CACHE" --model-name "$name" \
    --repository "$repo" --requested-revision "$revision" \
    --resolved-revision "$resolved_revision" >/dev/null
done

echo
echo "All downloads complete:"
du -sh models/* 2>/dev/null || true
echo "Next: make render && make serve"
