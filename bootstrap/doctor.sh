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
[[ "$wired" -ge 458752 ]] && ok "GPU wired limit: ${wired} MB" \
  || bad "GPU wired limit: ${wired} MB" "provision before install: sudo sysctl iogpu.wired_limit_mb=458752"

echo "== binaries =="
for spec in ".tools/bin/llama-server:re-run ./install with the policy-bound cache" \
            ".tools/bin/llama-swap:re-run ./install (bootstrap phase)" \
            ".tools/npm/bin/claude:re-run ./install (bootstrap phase)" \
            ".tools/npm/bin/opencode:re-run ./install (bootstrap phase)" \
            ".tools/npm/bin/kilo:re-run ./install (bootstrap phase)" \
            ".tools/bin/uv:re-run ./install with the policy-bound cache" \
            ".tools/bin/jq:re-run ./install with the policy-bound cache"; do
  b="${spec%%:*}"; fix="${spec#*:}"
  if [[ "$b" == */* ]]; then [[ -x "$b" ]] && ok "$b" || bad "$b missing" "$fix"
  else command -v "$b" >/dev/null && ok "$b" || bad "$b missing" "$fix"; fi
done
command -v oracle >/dev/null && ok "oracle on PATH" \
  || meh "oracle not on PATH" "add $HOME/.local/bin to PATH and re-run ./install"

echo "== lifecycle =="
python_bin=""
for candidate in "$ROOT/env/.venv/bin/python" python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    python_bin="$candidate"
    break
  fi
done
if [[ -n "$python_bin" ]]; then
  locked_python="$(awk -F= '$1 == "PYTHON_VERSION" {print $2}' "$ROOT/VERSIONS.lock" | awk '{print $1}')"
  actual_python="$("$python_bin" -c 'import platform; print(platform.python_version())')"
  if [[ "$actual_python" == "$locked_python" ]]; then
    ok "bootstrap Python trust root matches $locked_python"
  else
    bad "bootstrap Python is $actual_python, expected $locked_python" "provision the pinned Python runtime"
  fi
  if "$python_bin" verification/lifecycle.py validate-dependencies \
      --root "$ROOT" >/dev/null 2>&1; then
    ok "dependency pin policy valid"
  else
    bad "dependency pin policy invalid" \
      "$python_bin verification/lifecycle.py validate-dependencies --root \"$ROOT\""
  fi
  dependency_cache="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
  artifact_manifest="$dependency_cache/manifest.json"
  if [[ -f "$artifact_manifest" ]]; then
    if "$python_bin" verification/lifecycle.py validate-dependencies \
        --root "$ROOT" --manifest "$artifact_manifest" \
        --cache "$dependency_cache" --reproducible >/dev/null 2>&1; then
      ok "dependency-cache is policy-bound and reproducible"
    else
      bad "dependency-cache validation failed" \
        "re-export dependencies with bootstrap/export-dependencies.sh"
    fi
  else
    meh "dependency-cache manifest missing" \
      "export online artifacts before reproducible/offline install"
  fi
else
  bad "lifecycle checks need pinned Python" "provision the VERSIONS.lock Python trust root"
fi
state_path="$ROOT/.install-state/state.json"
if [[ -f "$state_path" ]]; then
  if jq -e --arg root "$ROOT" \
      '.schema_version == 1 and
       (.input_sha256 | test("^[0-9a-f]{64}$")) and
       .installation_root == $root and
       (.owned_paths | type == "array")' \
      "$state_path" >/dev/null 2>&1; then
    ok "install state records input hash and ownership"
  else
    bad "install state fields are invalid" "re-run ./install"
  fi
else
  meh "install state missing" "run ./install"
fi

echo "== platform scopes =="
POLICY="verification/policy.json"
if [[ -f "$POLICY" ]] && command -v jq >/dev/null 2>&1; then
  scope_count=0
  while IFS=$'\t' read -r scope_path scope_platform scope_reason; do
    [[ -n "$scope_path" ]] || continue
    ok "platform scope: $scope_path [$scope_platform] - $scope_reason"
    scope_count=$((scope_count+1))
  done < <(jq -r '.platform_scoped[] | [.path, .platform, .reason] | @tsv' "$POLICY")
  [[ "$scope_count" -gt 0 ]] || bad "platform scope policy is empty" "restore verification/policy.json"
else
  bad "platform scope policy unavailable" "restore verification/policy.json and install jq"
fi

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
[[ -f engines/kilo/hardened-env.sh ]] && ok "Kilo hardening profile present" || bad "Kilo hardening profile missing" "restore engines/kilo/hardened-env.sh"
kilo_cfg="$ROOT/state/generated/kilo/kilo.jsonc"
if [[ -f "$kilo_cfg" ]] && grep -q 'app\.kilo\.ai' "$kilo_cfg" 2>/dev/null; then
  bad "generated kilo.jsonc calls app.kilo.ai" "bash connectors/ide/sync-models.sh"
elif [[ -f "$kilo_cfg" ]]; then ok "generated kilo.jsonc has no cloud references"; fi

echo "== supabase (optional) =="
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q sentivue-supabase; then
  ok "supabase containers running"
else meh "supabase not running" "only needed for Postgres/pgvector: make supabase-up"; fi

echo
echo "doctor: $PASS pass, $WARN warn, $FAIL fail"
exit $((FAIL > 0 ? 1 : 0))
