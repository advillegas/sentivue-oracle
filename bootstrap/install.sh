#!/usr/bin/env bash
# SentiVue Oracle bootstrap for the Mac Studio.
# Connected acquisition is explicit; all downloaded roots remain checksum-bound.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source VERSIONS.lock
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
ARTIFACT_MANIFEST="$DEPENDENCY_CACHE/manifest.json"

find_python() {
  local candidate
  for candidate in "$ROOT/env/.venv/bin/python" \
      "$ROOT/.tools/python-bootstrap/bin/python3" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

artifact_path() {
  local artifact_id="$1" expected_request="$2" expected_resolved="${3:-}" python_bin
  local args
  python_bin="$(find_python)" || {
    echo "ERROR: Python is required to validate the dependency cache." >&2
    return 1
  }
  args=(artifact-path --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE"
    --artifact-id "$artifact_id" --expected-requested-version "$expected_request"
    --root "$ROOT" --reproducible)
  [[ -z "$expected_resolved" ]] || args+=(--expected-version "$expected_resolved")
  "$python_bin" "$ROOT/verification/lifecycle.py" "${args[@]}"
}

install_source_tree() {
  local artifact_id="$1" requested="$2" resolved="$3" destination="$4"
  local python_bin
  python_bin="$(find_python)" || return 1
  "$python_bin" "$ROOT/verification/lifecycle.py" preflight-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$destination" \
    --trusted-root "$ROOT" \
    --expected-version "$resolved" --expected-requested-version "$requested" \
    >/dev/null
  "$python_bin" "$ROOT/verification/lifecycle.py" install-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$destination" \
    --trusted-root "$ROOT" \
    --expected-version "$resolved" --expected-requested-version "$requested" \
    >/dev/null
  "$python_bin" "$ROOT/verification/lifecycle.py" validate-source \
    --root "$ROOT" --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --destination "$destination" \
    --trusted-root "$ROOT" \
    --expected-version "$resolved" --expected-requested-version "$requested" \
    >/dev/null
}

install_cached_binary() {
  local artifact_id="$1" requested="$2" resolved="$3" binary_name="$4" destination="$5"
  local archive stage candidate temporary
  archive="$(artifact_path "$artifact_id" "$requested" "$resolved")"
  stage="$(mktemp -d "$ROOT/.binary-stage.XXXXXX")"
  case "$archive" in
    *.zip) ditto -x -k "$archive" "$stage" ;;
    *.tar|*.tar.gz|*.tgz) tar -xf "$archive" -C "$stage" ;;
    *) cp "$archive" "$stage/$binary_name" ;;
  esac
  candidate="$(find "$stage" -type f -name "$binary_name" | head -1)"
  [[ -n "$candidate" ]] || {
    rm -rf "$stage"
    echo "ERROR: $artifact_id has no $binary_name" >&2
    return 1
  }
  mkdir -p "$(dirname "$destination")"
  temporary="${destination}.new"
  cp "$candidate" "$temporary"
  chmod +x "$temporary"
  mv -f "$temporary" "$destination"
  rm -rf "$stage"
}

if [[ "$(uname -s)/$(uname -m)" != "Darwin/arm64" && -z "${ORACLE_SKIP_OS_CHECK:-}" ]]; then
  echo "ERROR: deployment target is macOS arm64 (Mac Studio)."
  echo "       Set ORACLE_SKIP_OS_CHECK=1 to force (authoring machines only)."
  exit 1
fi

PYTHON_BIN="$(find_python)" || {
  echo "ERROR: Python 3.12+ is a platform prerequisite for offline validation." >&2
  exit 1
}
ACTUAL_PYTHON_VERSION="$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')"
[[ "$ACTUAL_PYTHON_VERSION" == "$PYTHON_VERSION" ]] || {
  echo "ERROR: bootstrap trust root requires Python $PYTHON_VERSION, found $ACTUAL_PYTHON_VERSION." >&2
  exit 1
}
CONNECTED_SETUP="${ORACLE_CONNECTED_SETUP:-0}"
if [[ "$CONNECTED_SETUP" == "1" ]]; then
  echo "==> acquiring promoted dependencies (connected, resumable)"
  ORACLE_PYTHON="$PYTHON_BIN" bash bootstrap/acquire-dependencies.sh
fi
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" validate-dependencies \
  --root "$ROOT"

echo "==> [1/8] Offline tool prerequisites"
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_FROM_API=1
export UV_CACHE_DIR="$DEPENDENCY_CACHE/uv"
export UV_TOOL_DIR="$ROOT/.tools/uv-tools"
export UV_TOOL_BIN_DIR="$ROOT/.tools/bin"
install_source_tree "node-darwin-arm64" "$NODE_VERSION" "$NODE_RESOLVED_VERSION" \
  "$ROOT/.tools/node"
node_binary="$(find "$ROOT/.tools/node" -type f -path '*/bin/node' | head -1)"
[[ -n "$node_binary" ]] || { echo "ERROR: cached Node tree has no bin/node" >&2; exit 1; }
export PATH="$(dirname "$node_binary"):$ROOT/.tools/bin:$PATH"
install_cached_binary "uv-darwin-arm64" "$UV_VERSION" "$UV_VERSION" uv \
  "$ROOT/.tools/bin/uv"
install_cached_binary "uv-darwin-arm64" "$UV_VERSION" "$UV_VERSION" uvx \
  "$ROOT/.tools/bin/uvx"
install_cached_binary "jq-darwin-arm64" "$JQ_VERSION" "$JQ_RESOLVED_VERSION" jq \
  "$ROOT/.tools/bin/jq"
install_cached_binary "lean-ctx-darwin-arm64" "$LEAN_CTX_VERSION" \
  "$LEAN_CTX_VERSION" lean-ctx "$ROOT/.tools/bin/lean-ctx"
mkdir -p "$ROOT/state/lean-ctx/config" "$ROOT/state/lean-ctx/data" \
  "$ROOT/state/lean-ctx/state" "$ROOT/state/lean-ctx/cache"
cp "$ROOT/engines/shared/lean-ctx-config.toml" \
  "$ROOT/state/lean-ctx/config/config.toml.new"
mv -f "$ROOT/state/lean-ctx/config/config.toml.new" \
  "$ROOT/state/lean-ctx/config/config.toml"
install_cached_binary "brew-llama-cpp" "$LLAMA_CPP_BREW_VERSION" \
  "$LLAMA_CPP_BREW_RESOLVED_VERSION" llama-server "$ROOT/.tools/bin/llama-server"
for required in tar node npm uv uvx jq lean-ctx llama-server; do
  command -v "$required" >/dev/null || {
    echo "ERROR: $required is absent; install it from the validated platform export." >&2
    exit 1
  }
done
[[ "$(lean-ctx --version)" == "lean-ctx ${LEAN_CTX_VERSION#v} "* ]] || {
  echo "ERROR: installed lean-ctx does not match $LEAN_CTX_VERSION." >&2
  exit 1
}
chmod +x bootstrap/*.sh serving/service.sh engines/*/launch.sh harness/ecc/install-ecc.sh \
         bin/* connectors/ide/*.sh connectors/gitea/*.sh

echo "==> [2/8] llama-swap ${LLAMA_SWAP_VERSION} (pinned release binary)"
mkdir -p .tools/bin
LLAMA_SWAP_ARCHIVE="$(
  artifact_path "llama-swap-darwin-arm64" "$LLAMA_SWAP_VERSION"
)"
rm -f .tools/bin/llama-swap
tar -xzf "$LLAMA_SWAP_ARCHIVE" -C .tools/bin llama-swap
chmod +x .tools/bin/llama-swap

echo "==> [3/8] Engines (pinned, repo-local npm prefix — nothing global)"
# npm install of Claude Code is deprecated upstream in favor of the native installer,
# but npm is the right choice here: exact version pin, no background auto-updates.
export npm_config_prefix="$ROOT/.tools/npm"
export npm_config_cache="$DEPENDENCY_CACHE/npm"
if [[ "$CONNECTED_SETUP" == "1" ]]; then
  export npm_config_offline=false
  unset UV_OFFLINE
  UV_MODE=()
else
  export npm_config_offline=true
  UV_MODE=(--offline)
fi
mkdir -p "$npm_config_prefix"
CLAUDE_ARCHIVE="$(artifact_path "npm-claude-code" "$CLAUDE_CODE_NPM_VERSION")"
OPENCODE_ARCHIVE="$(artifact_path "npm-opencode" "$OPENCODE_NPM_VERSION")"
KILO_ARCHIVE="$(artifact_path "npm-kilo-cli" "$KILO_CLI_NPM_VERSION")"
npm install -g "$CLAUDE_ARCHIVE" "$OPENCODE_ARCHIVE" "$KILO_ARCHIVE"

echo "==> [3b/8] 'oracle' CLI on PATH"
chmod +x bin/oracle
mkdir -p "$HOME/.local/bin"
ln -sfn "$ROOT/bin/oracle" "$HOME/.local/bin/oracle"
echo "    linked ~/.local/bin/oracle (ensure ~/.local/bin is on PATH)"

echo "==> [4/8] Python quant environment (uv)"
if [[ "$CONNECTED_SETUP" == "1" ]]; then
  ( cd env && uv sync --frozen )
else
  ( cd env && uv sync --offline --frozen )
fi
# Warm uvx caches only from policy-bound root artifacts; transitive wheels are
# resolved from the offline uv cache populated during explicit export.
MCP_DUCKDB_ARCHIVE="$(artifact_path "python-mcp-duckdb" "$MCP_DUCKDB")"
MCP_POSTGRES_ARCHIVE="$(artifact_path "python-mcp-postgres" "$MCP_POSTGRES")"
HF_CLI_ARCHIVE="$(
  artifact_path "hf-cli" "$HF_CLI_VERSION" "$HF_CLI_RESOLVED_VERSION"
)"
uvx "${UV_MODE[@]}" --from "$MCP_DUCKDB_ARCHIVE" mcp-server-duckdb --help >/dev/null
uvx "${UV_MODE[@]}" --from "$MCP_POSTGRES_ARCHIVE" postgres-mcp --help >/dev/null
uv tool install "${UV_MODE[@]}" "$HF_CLI_ARCHIVE"

echo "==> [5/8] Skills -> both engines"
bash bootstrap/sync-skills.sh

echo "==> [6/8] ECC ${ECC_PIN} curated subset"
install_source_tree "source-ecc" "$ECC_PIN" "$ECC_COMMIT" "$ROOT/harness/ecc/vendor"
bash harness/ecc/install-ecc.sh
echo "==> [6b/8] Skill packs: superpowers ${SUPERPOWERS_PIN} + gstack (pinned)"
install_source_tree "source-superpowers" "$SUPERPOWERS_PIN" "$SUPERPOWERS_COMMIT" \
  "$ROOT/harness/skill-packs/vendor/superpowers"
install_source_tree "source-gstack" "$GSTACK_PIN" "$GSTACK_COMMIT" \
  "$ROOT/harness/skill-packs/vendor/gstack"
bash harness/skill-packs/install-skill-packs.sh

echo "==> [7/8] Generated engine configuration deferred until model validation"

if [[ "$(uname -s)" == "Darwin" ]] && git --version >/dev/null 2>&1; then
  echo "==> [8/8] Local git vault (offline private remote + auto-backup target)"
  bash bootstrap/vault.sh init
elif [[ "$(uname -s)" == "Darwin" ]]; then
  echo "WARN: Apple Git is unavailable; vault and worktree missions remain disabled"
  echo "      until Command Line Tools are installed."
fi

mkdir -p memory logs reports state && touch memory/.gitkeep
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" state init \
  --root "$ROOT" --home "$HOME" >/dev/null
for owned_tree in "$ROOT/.tools" "$ROOT/env/.venv" \
  "$ROOT/harness/ecc/vendor" "$ROOT/harness/skill-packs/vendor"; do
  [[ -d "$owned_tree" ]] || {
    echo "ERROR: expected owned tree is missing: $owned_tree" >&2
    exit 1
  }
  "$PYTHON_BIN" "$ROOT/verification/lifecycle.py" state own-tree \
    --root "$ROOT" --home "$HOME" --path "$owned_tree"
done
[[ -e "$HOME/.local/bin/oracle" || -L "$HOME/.local/bin/oracle" ]] || {
  echo "ERROR: expected Oracle CLI link is missing" >&2
  exit 1
}
"$PYTHON_BIN" "$ROOT/verification/lifecycle.py" state own \
  --root "$ROOT" --home "$HOME" --path "$HOME/.local/bin/oracle"
echo
echo "Bootstrap complete; the guided installer will acquire the selected models next."
