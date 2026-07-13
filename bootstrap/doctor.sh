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
    if "$python_bin" verification/lifecycle.py validate-dependencies \
        --root "$ROOT" --manifest "$artifact_manifest" \
        --cache "$dependency_cache" --reproducible --include-optional \
        >/dev/null 2>&1; then
      ok "optional dependency exports are also resolved"
    else
      meh "optional dependency exports remain unresolved" \
        "import only the optional components needed on this platform"
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

echo "== serving =="
echo "read-only shared profile/resource/admission evidence:"
if [[ -n "$python_bin" ]]; then
  capability_json="$("$python_bin" verification/serving.py capabilities \
    --root "$ROOT" 2>&1)"
  capability_exit=$?
  if [[ $capability_exit -eq 0 ]]; then
    selected_backend="$(printf '%s' "$capability_json" | jq -r '.selected_backend')"
    capability_source="$(printf '%s' "$capability_json" | jq -r '.capability_source')"
    loaded_backend="$(printf '%s' "$capability_json" | jq -r '.loaded_backend // empty')"
    meh "selected backend: $selected_backend (inferred from $capability_source)" \
      "loaded/offloaded evidence is reported separately"
    if [[ -n "$loaded_backend" ]]; then
      offloaded="$(printf '%s' "$capability_json" | jq -r '.offloaded_layers')"
      ok "loaded backend: $loaded_backend; offloaded layers: $offloaded"
    else
      meh "loaded backend evidence unavailable" \
        "capability is not proof that llama.cpp loaded or offloaded"
    fi
    while IFS=$'\t' read -r model advertised slot parallel; do
      [[ -n "$model" ]] || continue
      ok "$model: advertised_context=$advertised, slot_context=$slot, parallel=$parallel"
    done < <(printf '%s' "$capability_json" | jq -r \
      '.admission.models // {} | to_entries[] |
       [.key, .value.advertised_context, .value.slot_context,
        .value.parallel_slots] | @tsv')
    if ! printf '%s' "$capability_json" | jq -e '.admission.models' >/dev/null; then
      meh "no generated admission plan" "oracle service install"
    fi
    tier_count="$(printf '%s' "$capability_json" | jq -r \
      '[.admission.tiers // {} | to_entries[].value] | unique | length')"
    tier_summary="$(printf '%s' "$capability_json" | jq -c \
      '.admission.tiers // {}')"
    if [[ "$tier_count" -eq 0 ]]; then
      meh "tier collapse evidence unavailable" \
        "generate an admission plan before certifying tiers"
    elif [[ "$tier_count" -lt 3 ]]; then
      meh "tier collapse: $tier_summary" \
        "reduced profiles may collapse tiers intentionally"
    else
      ok "tier mapping is distinct: $tier_summary"
    fi
  else
    bad "shared capability inspection failed: $capability_json" \
      "fix profile/resource declarations before serving"
  fi

  verify_json="$("$python_bin" verification/serving.py verify \
    --root "$ROOT" 2>&1)"
  verify_exit=$?
  if printf '%s' "$verify_json" | jq -e '.results' >/dev/null 2>&1; then
    loaded_status="$(printf '%s' "$verify_json" | jq -r \
      '[.results[] | select(.name == "loaded_backend")][0].status // "MISSING"')"
    loaded_reason="$(printf '%s' "$verify_json" | jq -r \
      '[.results[] | select(.name == "loaded_backend")][0].reason // "probe absent"')"
    if [[ "$loaded_status" == "PASS" ]]; then
      loaded_backend="$(printf '%s' "$verify_json" | jq -r \
        '[.results[] | select(.name == "loaded_backend")][0].evidence.loaded_backend')"
      offloaded="$(printf '%s' "$verify_json" | jq -r \
        '[.results[] | select(.name == "loaded_backend")][0].evidence.offloaded_layers')"
      ok "loaded backend $loaded_backend; offloaded layers $offloaded"
    elif [[ "$loaded_status" == "MISSING" ]]; then
      meh "loaded backend evidence is unavailable" \
        "the runtime did not return the loaded_backend probe"
    else
      meh "loaded backend evidence is provisional" "$loaded_reason"
    fi
  fi
  if [[ $verify_exit -eq 0 ]]; then
    ok "production-shaped serving verify probes PASS"
  elif [[ $verify_exit -eq 2 ]]; then
    meh "production-shaped serving verify is PROVISIONAL" \
      "headless engine flows or runtime evidence were skipped"
  else
    meh "production-shaped serving verify is not green" \
      "service may be down/unprovisioned; inspect shared verify output"
  fi
else
  bad "shared serving checks need pinned Python" \
    "provision the VERSIONS.lock Python trust root"
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
