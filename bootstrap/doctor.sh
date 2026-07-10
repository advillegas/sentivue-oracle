#!/usr/bin/env bash
# oracle doctor — full diagnostic with suggested fixes. Read-only, safe to run anytime.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PASS=0; FAIL=0; WARN=0
ok()   { printf ' \033[1;32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf ' \033[1;31mFAIL\033[0m  %s\n      fix: %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
meh()  { printf ' \033[1;33mWARN\033[0m  %s\n      %s\n' "$1" "$2"; WARN=$((WARN+1)); }

echo "== system =="
[[ "$(uname -s)/$(uname -m)" == "Darwin/arm64" ]] \
  && ok "macOS arm64" || bad "not macOS arm64" "this appliance targets the Mac Studio"
free_gb=$(df -g . 2>/dev/null | awk 'NR==2 {print $4}')
[[ "${free_gb:-0}" -gt 100 ]] && ok "disk free: ${free_gb} GB" \
  || meh "disk free: ${free_gb:-?} GB" "models + artifacts need headroom; consider pruning"
wired=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
[[ "$wired" -ge 400000 ]] && ok "GPU wired limit: ${wired} MB" \
  || meh "GPU wired limit: ${wired} MB (default)" "big models may not fit: sudo sysctl iogpu.wired_limit_mb=458752"

echo "== binaries =="
for spec in "llama-server:brew install llama.cpp" \
            ".tools/bin/llama-swap:re-run ./install (bootstrap phase)" \
            ".tools/npm/bin/claude:re-run ./install (bootstrap phase)" \
            ".tools/npm/bin/opencode:re-run ./install (bootstrap phase)" \
            "uv:brew install uv" "jq:brew install jq"; do
  b="${spec%%:*}"; fix="${spec#*:}"
  if [[ "$b" == */* ]]; then [[ -x "$b" ]] && ok "$b" || bad "$b missing" "$fix"
  else command -v "$b" >/dev/null && ok "$b" || bad "$b missing" "$fix"; fi
done
command -v oracle >/dev/null && ok "oracle on PATH" \
  || meh "oracle not on PATH" "ln -sf $ROOT/bin/oracle \$(brew --prefix)/bin/oracle"

echo "== models (per profile) =="
while IFS='|' read -r name _ _ slot _; do
  name="$(echo "$name" | xargs)"; slot="$(echo "$slot" | xargs)"; [[ -z "$name" ]] && continue
  if [[ -f serving/models.profile ]] && ! grep -qx "$name" serving/models.profile; then continue; fi
  f=$(find "models/$name" -name "*.gguf" -type f 2>/dev/null | head -1)
  [[ -n "$f" ]] && ok "$name ($slot): $(du -sh "models/$name" 2>/dev/null | cut -f1)" \
    || bad "$name ($slot) not downloaded" "oracle models"
done < <(grep -Ev '^\s*(#|$)' serving/models.manifest)

echo "== serving =="
[[ -f serving/llama-swap.rendered.yaml ]] && ok "rendered config" || bad "config not rendered" "oracle serve"
# tier map must point at models that exist on disk (a missing tier model silently
# degrades every engine to whatever is left — the "acting like 2022" failure)
if [[ -f serving/tiers.env ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" == *_MODEL && -n "$v" ]] || continue
    if [[ -n "$(find "models/$v" -name '*.gguf' -type f 2>/dev/null | head -1)" ]]; then
      ok "tier $k -> $v (on disk)"
    else
      bad "tier $k -> $v has no gguf on disk" "oracle models; then bash connectors/ide/sync-models.sh"
    fi
  done < serving/tiers.env
else
  meh "serving/tiers.env missing" "run ./install or connectors/ide/sync-models.sh"
fi
HAIKU="$(sed -n 's/^HAIKU_MODEL=//p' serving/tiers.env 2>/dev/null | head -1)"
HAIKU="${HAIKU:-qwen3-coder-30b}"
if curl -sf -m 5 http://127.0.0.1:9099/health >/dev/null 2>&1; then
  ok "llama-swap healthy"
  # loopback-only binding is a privacy invariant, not an assumption — verify it
  if lsof -nP -iTCP:9099 -sTCP:LISTEN 2>/dev/null | grep -qE '(\*|0\.0\.0\.0|\[::\]):9099'; then
    bad "llama-swap listening on ALL interfaces" "service must use --listen 127.0.0.1:9099 (serving/service.sh)"
  else
    ok "llama-swap bound to loopback only"
  fi
  r=$(curl -sf -m 60 http://127.0.0.1:9099/v1/chat/completions -H 'Content-Type: application/json' \
    -d "{\"model\":\"$HAIKU\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}]}" \
    | jq -r '.choices[0].message.content' 2>/dev/null)
  [[ -n "$r" && "$r" != "null" ]] && ok "fast lane inference ($HAIKU): $r" || bad "fast lane not answering" "oracle restart; tail logs/llama-swap.err.log"
  # production-shaped probe: agent sessions open with >25k tokens; a serving stack
  # that only answers hello-sized prompts is unusable no matter how healthy it looks
  bigprompt=$(printf 'lorem ipsum dolor sit amet %.0s' $(seq 1 5200))
  code=$(curl -s -o /tmp/oracle-ctx-probe.json -w '%{http_code}' -m 300 \
    http://127.0.0.1:9099/v1/chat/completions -H 'Content-Type: application/json' \
    -d "{\"model\":\"$HAIKU\",\"max_tokens\":4,\"messages\":[{\"role\":\"user\",\"content\":\"$bigprompt\"}]}")
  if [[ "$code" == "200" ]]; then ok "agent-scale context probe (~26k tokens) accepted"
  else bad "agent-scale context probe rejected (HTTP $code): $(head -c 120 /tmp/oracle-ctx-probe.json 2>/dev/null)" \
           "context split across --parallel slots? one 32k slot beats two 8k slots"
  fi
else
  bad "llama-swap not responding" "oracle serve; then tail logs/llama-swap.err.log"
fi

echo "== engines =="
[[ -f engines/claude-code/home/settings.json ]] && ok "claude config" || bad "claude config missing" "git checkout engines/"
[[ -f engines/opencode/xdg/opencode/opencode.json ]] && ok "opencode config" || bad "opencode config missing" "git checkout engines/"
n=$(ls skills/*/SKILL.md 2>/dev/null | wc -l | xargs)
[[ "$n" -ge 10 ]] && ok "skills: $n packs" || meh "skills: $n packs" "oracle skills"
grep -q DISABLE_TELEMETRY engines/claude-code/home/settings.json 2>/dev/null \
  && ok "claude telemetry disabled" || bad "telemetry env missing" "restore settings.json"

echo "== git vault (offline private remote) =="
if git remote get-url vault >/dev/null 2>&1; then
  ok "vault remote -> $(git remote get-url vault)"
  git fetch --quiet vault 2>/dev/null || true
  behind=$(git rev-list --count vault/main..main 2>/dev/null || echo "?")
  if [[ "$behind" == "0" ]]; then ok "vault main is current"
  else meh "vault main behind by $behind commit(s)" "oracle vault sync"; fi
else
  meh "no vault remote configured" "oracle vault init"
fi

echo "== privacy =="
if sudo -n pfctl -sr 2>/dev/null | grep -q "block drop out"; then ok "pf egress block ACTIVE (air-gapped)"
else meh "pf egress block not active" "optional: oracle harden"; fi

echo "== supabase (optional) =="
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q sentivue-supabase; then
  ok "supabase containers running"
else meh "supabase not running" "only needed for Postgres/pgvector: make supabase-up"; fi

echo
echo "doctor: $PASS pass, $WARN warn, $FAIL fail"
exit $((FAIL > 0 ? 1 : 0))
