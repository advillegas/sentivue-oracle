#!/usr/bin/env bash
# End-to-end smoke test. Run with networking disabled to prove the offline posture.
# FULL=1 also exercises the big slot (triggers a multi-minute model load).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
BASE="http://127.0.0.1:9099"

echo "== binaries =="
for b in .tools/bin/llama-swap .tools/npm/bin/claude .tools/npm/bin/opencode; do
  [[ -x "$b" ]] && ok "$b" || bad "$b missing"
done
command -v llama-server >/dev/null && ok "llama-server" || bad "llama-server missing"

echo "== service =="
curl -sf -m 5 "$BASE/health" >/dev/null && ok "llama-swap health" || bad "llama-swap down (make serve)"

echo "== fast lane: OpenAI wire (OpenCode path) =="
r=$(curl -sf -m 120 "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -d '{
  "model":"qwen3-coder-30b","max_tokens":20,
  "messages":[{"role":"user","content":"Reply with exactly: ORACLE-OK"}]}' | jq -r '.choices[0].message.content' 2>/dev/null)
[[ "$r" == *ORACLE-OK* ]] && ok "chat/completions -> $r" || bad "chat/completions -> ${r:-no response}"

echo "== fast lane: Anthropic wire (Claude Code path) =="
r=$(curl -sf -m 120 "$BASE/v1/messages" -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' -d '{
  "model":"qwen3-coder-30b","max_tokens":20,
  "messages":[{"role":"user","content":"Reply with exactly: ORACLE-OK"}]}' | jq -r '.content[0].text' 2>/dev/null)
[[ "$r" == *ORACLE-OK* ]] && ok "v1/messages -> $r" || bad "v1/messages -> ${r:-no response}"

echo "== embeddings =="
n=$(curl -sf -m 60 "$BASE/v1/embeddings" -H 'Content-Type: application/json' -d '{
  "model":"qwen3-embedding-4b","input":"drawdown"}' | jq '.data[0].embedding | length' 2>/dev/null)
[[ "${n:-0}" -gt 100 ]] && ok "embeddings dim=$n" || bad "embeddings"

first_big() {   # first big-slot model active under the current profile
  while IFS='|' read -r name _ _ slot _; do
    name="$(echo "$name" | xargs)"; slot="$(echo "$slot" | xargs)"
    [[ "$slot" == "big" ]] || continue
    if [[ ! -f serving/models.profile ]] || grep -qx "$name" serving/models.profile; then
      echo "$name"; return
    fi
  done < <(grep -Ev '^\s*(#|$)' serving/models.manifest)
}

if [[ "${FULL:-0}" == "1" ]]; then
  BIG="$(first_big)"
  if [[ -z "$BIG" ]]; then
    echo "== big slot: none in this profile — skipping =="
  else
    echo "== big slot: $BIG (hundreds of GB load from SSD, be patient) =="
    r=$(curl -sf -m 1800 "$BASE/v1/messages" -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' -d '{
      "model":"'"$BIG"'","max_tokens":30,
      "messages":[{"role":"user","content":"Reply with exactly: BIG-SLOT-OK"}]}' | jq -r '.content[0].text' 2>/dev/null)
    [[ "$r" == *BIG-SLOT-OK* ]] && ok "big slot -> $r" || bad "big slot ($BIG)"
  fi
fi

echo "== engines headless =="
r=$(bash engines/claude-code/launch.sh -p "Reply with exactly: ENGINE-OK" --model qwen3-coder-30b 2>/dev/null | tail -1)
[[ "$r" == *ENGINE-OK* ]] && ok "claude -p" || bad "claude -p -> ${r:-no output}"
r=$(bash engines/opencode/launch.sh run -m oracle/qwen3-coder-30b "Reply with exactly: ENGINE-OK" 2>/dev/null | tail -1)
[[ "$r" == *ENGINE-OK* ]] && ok "opencode run" || bad "opencode run -> ${r:-no output}"

echo
echo "verify: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && echo "Stack is operational. Safe to disconnect from the network (or run: make harden)."
exit $FAIL
