#!/usr/bin/env bash
# sync-models.sh - auto-detect the models actually on this machine and point
# every surface at them (IDE extensions + engine tier maps). Safe to run any
# time; runs on every IDE launch. Stock macOS bash 3.2 compatible.
#
# Detection order (first that answers wins):
#   1. live llama-swap  GET http://127.0.0.1:9099/v1/models
#   2. disk scan        models/<name>/**/*.gguf (anything downloaded is real)
#
# Writes:
#   ~/.continue/config.yaml       one entry per detected model, roles by slot
#   ~/.config/kilo/kilo.jsonc     Kilo Code global config (local provider + models,
#                                 telemetry off, sharing off)
#   <root>/serving/tiers.env      opus/sonnet/haiku remapped onto models that
#                                 exist (engine launchers + conductor read this)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
API_BASE="http://127.0.0.1:9099/v1"
MANIFEST="$ROOT/serving/models.manifest"

slot_of() {  # slot_of <model-name>
  awk -F'|' -v n="$1" '$0 !~ /^[[:space:]]*#/ && NF >= 4 {
    m=$1; gsub(/^[ \t]+|[ \t]+$/, "", m)
    if (m == n) { s=$4; gsub(/^[ \t]+|[ \t]+$/, "", s); print s; exit }
  }' "$MANIFEST"
}
ctx_of() {  # ctx_of <model-name>
  awk -F'|' -v n="$1" '$0 !~ /^[[:space:]]*#/ && NF >= 5 {
    m=$1; gsub(/^[ \t]+|[ \t]+$/, "", m)
    if (m == n) { c=$5; gsub(/^[ \t]+|[ \t]+$/, "", c); print c; exit }
  }' "$MANIFEST"
}
manifest_names() {
  awk -F'|' '$0 !~ /^[[:space:]]*#/ && NF >= 4 {
    m=$1; gsub(/^[ \t]+|[ \t]+$/, "", m); if (m != "") print m
  }' "$MANIFEST"
}
tier_from_env() {  # tier_from_env <KEY>
  [[ -f "$ROOT/serving/tiers.env" ]] && sed -n "s/^$1=//p" "$ROOT/serving/tiers.env" | head -1 | xargs || true
}
in_list() {  # in_list <needle> <items...>
  local n="$1"; shift
  for x in "$@"; do [[ "$x" == "$n" ]] && return 0; done
  return 1
}

# ---- detect -----------------------------------------------------------------
ids=() source=""
live="$(curl -sf -m 3 "$API_BASE/models" 2>/dev/null || true)"
if [[ -n "$live" ]]; then
  if command -v jq >/dev/null; then
    while IFS= read -r m; do [[ -n "$m" ]] && ids+=("$m"); done < <(echo "$live" | jq -r '.data[].id')
  else
    while IFS= read -r m; do [[ -n "$m" ]] && ids+=("$m"); done < <(echo "$live" | grep -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/')
  fi
  [[ ${#ids[@]} -gt 0 ]] && source="live llama-swap"
fi

if [[ ${#ids[@]} -eq 0 ]]; then
  # anything with a .gguf on disk was downloaded on purpose - serve it
  while IFS= read -r name; do
    if [[ -n "$(find "$ROOT/models/$name" -name '*.gguf' -type f 2>/dev/null | head -1)" ]]; then
      ids+=("$name")
    fi
  done < <(manifest_names)
  [[ ${#ids[@]} -gt 0 ]] && source="disk scan"
fi

if [[ ${#ids[@]} -eq 0 ]]; then
  echo "sync-models: no models detected (download models first) - configs left untouched"
  exit 0
fi

# ---- tier mapping onto what actually exists ----------------------------------
# Preference order per tier: models the install profile intended, then slot fit,
# then anything chat-capable. A stale tiers.env never pins a tier to a lesser
# model once the intended one shows up on disk.
chat=()
for m in "${ids[@]}"; do
  [[ "$(slot_of "$m")" == "embed" ]] && continue
  chat+=("$m")
done

PROFILE_LIST=""
[[ -f "$ROOT/serving/models.profile" ]] && PROFILE_LIST="$(grep -v '^#' "$ROOT/serving/models.profile" | sed '/^$/d')"
profile_has() {  # no profile file => everything is intended
  [[ -z "$PROFILE_LIST" ]] && return 0
  grep -qx "$1" <<<"$PROFILE_LIST"
}
pick_tier() {  # pick_tier <wanted> <slot-pref...>
  local wanted="$1"; shift
  if [[ -n "$wanted" ]] && in_list "$wanted" "${chat[@]:-}" && profile_has "$wanted"; then
    echo "$wanted"; return
  fi
  local inprof s m
  for inprof in 1 0; do
    for s in "$@"; do
      for m in "${chat[@]:-}"; do
        [[ -n "$m" && "$(slot_of "$m")" == "$s" ]] || continue
        if [[ $inprof -eq 1 ]]; then profile_has "$m" || continue
        else profile_has "$m" && continue; fi
        echo "$m"; return
      done
    done
  done
  echo "${chat[0]:-}"
}

sonnet="$(pick_tier "$(tier_from_env SONNET_MODEL)" fast big)"
opus="$(pick_tier "$(tier_from_env OPUS_MODEL)" big fast)"
# haiku = the smallest fast model that can HOLD AN AGENT SESSION (ctx >= 32k).
# A separate small process protects the primary model's prefix cache, but a
# model too small for engine sessions is worse than eviction (16k 7B died on
# tool grammar + context floor, FAILURES 2026-07-11). No qualifying small
# model => haiku rides the sonnet model.
haiku=""
smallest=""
smallest_size=0
for m in "${chat[@]:-}"; do
  [[ -n "$m" && "$(slot_of "$m")" == "fast" ]] || continue
  ctx_m="$(ctx_of "$m")"
  [[ "$ctx_m" =~ ^[0-9]+$ && "$ctx_m" -ge 32768 ]] || continue
  sz="$(find "$ROOT/models/$m" -name '*.gguf' -type f -exec stat -f%z {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')"
  [[ "$sz" -gt 0 ]] || sz="$(find "$ROOT/models/$m" -name '*.gguf' -type f -exec stat -c%s {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')"
  if [[ "$sz" -gt 0 && ( -z "$smallest" || "$sz" -lt "$smallest_size" ) ]]; then
    smallest="$m"; smallest_size="$sz"
  fi
done
[[ -n "$smallest" ]] && haiku="$smallest"
[[ -z "$haiku" ]] && haiku="$sonnet"
[[ -z "$haiku" ]] && haiku="$(pick_tier "$(tier_from_env HAIKU_MODEL)" fast big)"

anchor="$sonnet"
[[ -z "$anchor" ]] && anchor="${ids[0]}"
ordered=("$anchor")
for m in "${ids[@]}"; do [[ "$m" != "$anchor" ]] && ordered+=("$m"); done

# ---- tiers.env + engine configs: remap tiers onto models that exist ----------
if [[ -n "$sonnet" ]]; then
  printf 'OPUS_MODEL=%s\nSONNET_MODEL=%s\nHAIKU_MODEL=%s\n' "$opus" "$sonnet" "$haiku" > "$ROOT/serving/tiers.env"
  if command -v jq >/dev/null; then
    S="$ROOT/engines/claude-code/home/settings.json"
    if [[ -f "$S" ]]; then
      jq --arg o "$opus" --arg s "$sonnet" --arg h "$haiku" '
         .env.ANTHROPIC_DEFAULT_OPUS_MODEL=$o | .env.ANTHROPIC_DEFAULT_SONNET_MODEL=$s |
         .env.ANTHROPIC_DEFAULT_HAIKU_MODEL=$h | .env.ANTHROPIC_MODEL=$s |
         .env.ANTHROPIC_SMALL_FAST_MODEL=$h  | .model=$s' "$S" > "$S.tmp" && mv "$S.tmp" "$S"
    fi
    O="$ROOT/engines/opencode/xdg/opencode/opencode.json"
    if [[ -f "$O" ]]; then
      # OpenCode only offers models declared in the provider map - rebuild it
      # from the detected chat models so the picker matches the machine.
      mm="{}"
      for id in "${chat[@]}"; do
        ctx="$(ctx_of "$id")"; [[ "$ctx" =~ ^[0-9]+$ && "$ctx" -gt 0 ]] || ctx=32768
        out=$(( ctx / 2 )); (( out < 8192 )) && out=8192; (( out > 65536 )) && out=65536
        r=false; [[ "$id" == *thinking* ]] && r=true
        mm="$(echo "$mm" | jq --arg id "$id" --argjson ctx "$ctx" --argjson out "$out" --argjson r "$r" \
          '.[$id] = ({name: ($id + " (local)"), tool_call: true}
                     + (if $r then {reasoning: true} else {} end)
                     + {limit: {context: $ctx, output: $out}})')"
      done
      jq --argjson mm "$mm" --arg s "oracle/$sonnet" --arg h "oracle/$haiku" \
         '.provider.oracle.models=$mm | .model=$s | .small_model=$h' "$O" > "$O.tmp" && mv "$O.tmp" "$O"
    fi
  fi
  # OpenCode agent personas: remap each role's model line onto this machine's tiers
  AGENT_DIR="$ROOT/engines/opencode/xdg/opencode/agent"
  set_agent_model() {  # set_agent_model <file> <model>
    [[ -f "$AGENT_DIR/$1" ]] || return 0
    sed -i '' "s|^model: oracle/.*|model: oracle/$2|" "$AGENT_DIR/$1" 2>/dev/null || \
      sed -i "s|^model: oracle/.*|model: oracle/$2|" "$AGENT_DIR/$1"
  }
  set_agent_model researcher.md "$haiku"
  set_agent_model auditor.md    "$haiku"
  set_agent_model librarian.md  "$haiku"
  set_agent_model developer.md  "$sonnet"
  set_agent_model envoy.md      "$sonnet"
  set_agent_model adversary.md  "$opus"
else
  echo "sync-models: WARNING - only embedding models found; download a chat model"
fi

# ---- Continue: ~/.continue/config.yaml ---------------------------------------
# systemMessage grounds small local models: without it they fall back to 2022-era
# chatbot habits ("As an AI I cannot run commands..."). capabilities: [tool_use]
# unlocks Continue's Agent mode so the model can actually edit files + run
# terminal commands through the IDE.
mkdir -p "$HOME/.continue"
{
  echo "# GENERATED by sync-models.sh - models auto-detected from $source."
  echo "# Regenerated on every IDE launch."
  echo "name: SentiVue Oracle"
  echo "version: 1.0.0"
  echo "models:"
  for id in "${ordered[@]}"; do
    slot="$(slot_of "$id")"; ctx="$(ctx_of "$id")"
    case "$slot" in
      fast)  roles="[chat, edit, apply, autocomplete]" ;;
      embed) roles="[embed]" ;;
      *)     roles="[chat, edit, apply]" ;;
    esac
    echo "  - name: $id (local)"
    echo "    provider: openai"
    echo "    model: $id"
    echo "    apiBase: $API_BASE"
    echo "    apiKey: oracle-local"
    echo "    roles: $roles"
    if [[ "$slot" != "embed" ]]; then
      echo "    capabilities: [tool_use]"
      echo "    systemMessage: |"
      echo "      You are SentiVue Oracle, a senior software engineer running 100% locally on the user's machine - private, offline, no cloud."
      echo "      It is 2026. Behave like a capable coding agent, not a chatbot."
      echo "      In Agent mode you have real tools: create and edit files, run terminal commands, read their output, and verify results yourself instead of instructing the user."
      echo "      Never claim you lack access to the machine. If tools are unavailable (plain Chat mode), give the exact code or command once - no tutorials, no 'go to python.org'."
      echo "      Style: direct and concise. No greetings, no apologies, never 'As an AI'. Bias to action: implement, run, verify, report. Make reasonable assumptions and state them in one line."
    fi
    if [[ "$ctx" =~ ^[0-9]+$ && "$ctx" -gt 0 ]]; then
      echo "    defaultCompletionOptions:"
      echo "      contextLength: $ctx"
    fi
  done
} > "$HOME/.continue/config.yaml"

# ---- Kilo Code: global JSONC config (all Kilo surfaces read this) -------------
if [[ ${#chat[@]} -gt 0 ]] && command -v jq >/dev/null; then
  mm="{}"
  for id in "${chat[@]}"; do
    ctx="$(ctx_of "$id")"; [[ "$ctx" =~ ^[0-9]+$ && "$ctx" -gt 0 ]] || ctx=32768
    out=$(( ctx / 2 )); (( out < 8192 )) && out=8192; (( out > 65536 )) && out=65536
    r=false; [[ "$id" == *thinking* ]] && r=true
    mm="$(echo "$mm" | jq --arg id "$id" --argjson ctx "$ctx" --argjson out "$out" --argjson r "$r" \
      '.[$id] = ({name: ($id + " (local)"), tool_call: true}
                 + (if $r then {reasoning: true} else {} end)
                 + {limit: {context: $ctx, output: $out}})')"
  done
  mkdir -p "$HOME/.config/kilo"
  {
    echo "// GENERATED by sync-models.sh - regenerated on every IDE launch"
    jq -n --argjson mm "$mm" --arg m "openai-compatible/$anchor" --arg base "$API_BASE" \
       --arg ide "$ROOT/engines/shared/IDE-AGENT.md" \
       --arg conv "$ROOT/engines/shared/CONVENTIONS.md" --arg auto "$ROOT/engines/shared/AUTONOMY.md" '
      {"$schema": "https://app.kilo.ai/config.json",
       model: $m,
       share: "disabled",
       enabled_providers: ["openai-compatible"],
       instructions: [$ide, $conv, $auto],
       provider: {"openai-compatible": {
         options: {apiKey: "oracle-local", baseURL: $base},
         models: $mm}},
       permission: {edit: "allow", bash: "allow", webfetch: "deny"},
       experimental: {openTelemetry: false}}'
  } > "$HOME/.config/kilo/kilo.jsonc"
fi

echo "sync-models: ${#ordered[@]} model(s) from $source (opus=$opus sonnet=$sonnet haiku=$haiku)"
echo "  ${ordered[*]}"
