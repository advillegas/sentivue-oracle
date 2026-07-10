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
chat=() fast=() big=()
for m in "${ids[@]}"; do
  s="$(slot_of "$m")"
  [[ "$s" == "embed" ]] && continue
  chat+=("$m")
  [[ "$s" == "fast" ]] && fast+=("$m")
  [[ "$s" == "big" ]] && big+=("$m")
done

sonnet="$(tier_from_env SONNET_MODEL)"
in_list "${sonnet:-}" "${chat[@]:-}" || sonnet=""
[[ -z "$sonnet" && ${#fast[@]} -gt 0 ]] && sonnet="${fast[0]}"
[[ -z "$sonnet" && ${#big[@]}  -gt 0 ]] && sonnet="${big[0]}"
[[ -z "$sonnet" && ${#chat[@]} -gt 0 ]] && sonnet="${chat[0]}"

opus="$(tier_from_env OPUS_MODEL)"
in_list "${opus:-}" "${chat[@]:-}" || opus=""
[[ -z "$opus" && ${#big[@]} -gt 0 ]] && opus="${big[0]}"
[[ -z "$opus" ]] && opus="$sonnet"

haiku="$(tier_from_env HAIKU_MODEL)"
in_list "${haiku:-}" "${chat[@]:-}" || haiku=""
[[ -z "$haiku" && ${#fast[@]} -gt 0 ]] && haiku="${fast[0]}"
[[ -z "$haiku" ]] && haiku="$sonnet"

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
  ADV="$ROOT/engines/opencode/xdg/opencode/agent/adversary.md"
  if [[ -f "$ADV" ]]; then
    sed -i '' "s|^model: oracle/.*|model: oracle/$opus|" "$ADV" 2>/dev/null || \
      sed -i "s|^model: oracle/.*|model: oracle/$opus|" "$ADV"
  fi
else
  echo "sync-models: WARNING - only embedding models found; download a chat model"
fi

# ---- Continue: ~/.continue/config.yaml ---------------------------------------
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
       --arg conv "$ROOT/engines/shared/CONVENTIONS.md" --arg auto "$ROOT/engines/shared/AUTONOMY.md" '
      {"$schema": "https://app.kilo.ai/config.json",
       model: $m,
       share: "disabled",
       enabled_providers: ["openai-compatible"],
       instructions: [$conv, $auto],
       provider: {"openai-compatible": {
         options: {apiKey: "oracle-local", baseURL: $base},
         models: $mm}},
       experimental: {openTelemetry: false}}'
  } > "$HOME/.config/kilo/kilo.jsonc"
fi

echo "sync-models: ${#ordered[@]} model(s) from $source (opus=$opus sonnet=$sonnet haiku=$haiku)"
echo "  ${ordered[*]}"
