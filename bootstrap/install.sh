#!/usr/bin/env bash
# SentiVue Oracle — one-time ONLINE bootstrap for the Mac Studio.
# Everything after this (plus `make models`) runs fully offline.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source VERSIONS.lock
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
ARTIFACT_MANIFEST="$DEPENDENCY_CACHE/manifest.json"

find_python() {
  local candidate
  for candidate in "$ROOT/env/.venv/bin/python" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

artifact_path() {
  local artifact_id="$1" expected_version="$2" python_bin
  python_bin="$(find_python)" || {
    echo "ERROR: Python is required to validate the dependency cache." >&2
    return 1
  }
  "$python_bin" "$ROOT/verification/lifecycle.py" artifact-path \
    --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE" \
    --artifact-id "$artifact_id" --expected-version "$expected_version" \
    --expected-requested-version "$expected_version"
}

if [[ "$(uname -s)/$(uname -m)" != "Darwin/arm64" && -z "${ORACLE_SKIP_OS_CHECK:-}" ]]; then
  echo "ERROR: deployment target is macOS arm64 (Mac Studio)."
  echo "       Set ORACLE_SKIP_OS_CHECK=1 to force (authoring machines only)."
  exit 1
fi

echo "==> [1/8] Homebrew packages"
command -v brew >/dev/null || { echo "ERROR: install Homebrew first: https://brew.sh"; exit 1; }
export HOMEBREW_CACHE="$DEPENDENCY_CACHE/homebrew"
export HOMEBREW_NO_AUTO_UPDATE=1
brew install "$LLAMA_CPP_BREW_FORMULA" "node@${NODE_MAJOR}" uv jq git gettext || true
brew pin "$LLAMA_CPP_BREW_FORMULA" || true
# node@N is keg-only: without linking, npm/node are NOT on PATH on a clean Mac
if ! command -v node >/dev/null; then
  brew link --overwrite --force "node@${NODE_MAJOR}" || true
fi
command -v node >/dev/null || export PATH="$(brew --prefix)/opt/node@${NODE_MAJOR}/bin:$PATH"
command -v npm >/dev/null || { echo "ERROR: npm still not on PATH after linking node@${NODE_MAJOR}"; exit 1; }
chmod +x bootstrap/*.sh serving/service.sh engines/*/launch.sh harness/ecc/install-ecc.sh \
         bin/* connectors/ide/*.sh connectors/gitea/*.sh 2>/dev/null || true

echo "==> [2/8] llama-swap ${LLAMA_SWAP_VERSION} (pinned release binary)"
mkdir -p .tools/bin
if [[ ! -x .tools/bin/llama-swap ]]; then
  LLAMA_SWAP_ARCHIVE="$(
    artifact_path "llama-swap-darwin-arm64" "$LLAMA_SWAP_VERSION"
  )"
  tar -xzf "$LLAMA_SWAP_ARCHIVE" -C .tools/bin llama-swap
  chmod +x .tools/bin/llama-swap
fi

echo "==> [3/8] Engines (pinned, repo-local npm prefix — nothing global)"
# npm install of Claude Code is deprecated upstream in favor of the native installer,
# but npm is the right choice here: exact version pin, no background auto-updates.
export npm_config_prefix="$ROOT/.tools/npm"
export npm_config_cache="$DEPENDENCY_CACHE/npm"
export npm_config_offline=true
mkdir -p "$npm_config_prefix"
CLAUDE_ARCHIVE="$(artifact_path "npm-claude-code" "$CLAUDE_CODE_NPM_VERSION")"
OPENCODE_ARCHIVE="$(artifact_path "npm-opencode" "$OPENCODE_NPM_VERSION")"
KILO_ARCHIVE="$(artifact_path "npm-kilo-cli" "$KILO_CLI_NPM_VERSION")"
npm install -g "$CLAUDE_ARCHIVE" "$OPENCODE_ARCHIVE" "$KILO_ARCHIVE"

echo "==> [3b/8] 'oracle' CLI on PATH"
chmod +x bin/oracle
BREW_BIN="$(brew --prefix)/bin"
if ln -sf "$ROOT/bin/oracle" "$BREW_BIN/oracle" 2>/dev/null; then
  echo "    linked $BREW_BIN/oracle"
else
  mkdir -p "$HOME/.local/bin" && ln -sf "$ROOT/bin/oracle" "$HOME/.local/bin/oracle"
  echo "    linked ~/.local/bin/oracle (ensure ~/.local/bin is on PATH)"
fi

echo "==> [4/8] Python quant environment (uv)"
( cd env && uv sync --offline --frozen )
# Warm uvx caches for the MCP servers so they launch offline later.
uvx --offline --from "$MCP_DUCKDB" mcp-server-duckdb --help >/dev/null 2>&1 || true
uvx --offline --from "$MCP_POSTGRES" postgres-mcp --help  >/dev/null 2>&1 || true
uv tool install --offline "${HF_CLI}" 2>/dev/null || true

echo "==> [5/8] Skills -> both engines"
bash bootstrap/sync-skills.sh

echo "==> [6/8] ECC ${ECC_PIN} curated subset"
bash harness/ecc/install-ecc.sh
echo "==> [6b/8] Skill packs: superpowers ${SUPERPOWERS_PIN} + gstack (pinned)"
bash harness/skill-packs/install-skill-packs.sh

echo "==> [7/8] Warm OpenCode model catalog cache (offline use later)"
export XDG_CONFIG_HOME="$ROOT/engines/opencode/xdg"
export XDG_DATA_HOME="$ROOT/engines/opencode/xdg-data"
"$npm_config_prefix/bin/opencode" models >/dev/null 2>&1 || true

echo "==> [8/8] GPU wired limit (448 GB for models, needs sudo; persists via LaunchDaemon)"
if sudo -n true 2>/dev/null || sudo -v; then
  sudo sysctl "iogpu.wired_limit_mb=458752" || true
  sudo tee /Library/LaunchDaemons/com.sentivue.wiredlimit.plist >/dev/null <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.sentivue.wiredlimit</string>
  <key>ProgramArguments</key><array>
    <string>/usr/sbin/sysctl</string><string>iogpu.wired_limit_mb=458752</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.sentivue.wiredlimit.plist 2>/dev/null || true
else
  echo "WARN: skipped wired-limit (no sudo). Run manually: sudo sysctl iogpu.wired_limit_mb=458752"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "==> [8b/8] Local git vault (offline private remote + auto-backup target)"
  bash bootstrap/vault.sh init || echo "WARN: vault init failed — run 'oracle vault init' later"
fi

mkdir -p memory logs reports state && touch memory/.gitkeep
echo
echo "Bootstrap complete. Next:"
echo "  make models   (~700 GB download, resumable)"
echo "  make serve && make verify"
