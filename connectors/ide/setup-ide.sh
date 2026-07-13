#!/usr/bin/env bash
# The Cursor-like IDE for the appliance: VSCodium (telemetry-free VS Code) with
# Continue (chat / inline edit / autocomplete) and Kilo Code (agentic side panel),
# all pointed at the local llama-swap endpoint. Fully offline after install.
# (Kilo Code replaced Roo Code, which was discontinued May 2026.)
#
#   bash connectors/ide/setup-ide.sh install    cask + pinned .vsix + configs
#   bash connectors/ide/setup-ide.sh launch     open the IDE on this repo
#
# Cursor-parity map: chat with codebase -> Continue chat (Cmd+L); inline edits ->
# Continue edit (Cmd+I); tab autocomplete -> Continue (fast lane model); agent
# composer -> Kilo Code panel (reads/writes files, runs terminal, plan/act);
# parallel agent tabs -> Cmd+Shift+A opens an engine session as an editor tab,
# any number at once, each optionally in its own git worktree (no collisions).
# Models are auto-detected from the machine on every launch (sync-models.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ORACLE_ROOT="$ROOT"
export ORACLE_PROJECT_ROOT="$ROOT"
# shellcheck source=/dev/null
source "$ROOT/engines/shared/lean-ctx-env.sh"
export PATH="$ROOT/env/.venv/bin:$ROOT/.tools/bin:$PATH"
VSCODIUM_ROOT="$ROOT/.tools/vscodium"
VSCODIUM_APP="$VSCODIUM_ROOT/VSCodium.app"
CODIUM_BIN="$VSCODIUM_APP/Contents/Resources/app/bin/codium"
EXTENSIONS_DIR="$VSCODIUM_ROOT/extensions"
USER_DATA_DIR="$ROOT/state/generated/vscodium"
VSIX_DIR="$VSCODIUM_ROOT/vsix"
source "$ROOT/VERSIONS.lock"
DEPENDENCY_CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
ARTIFACT_MANIFEST="$DEPENDENCY_CACHE/manifest.json"

artifact_path() {
  local artifact_id="$1" expected_version="$2" python_bin args
  for python_bin in "$ROOT/env/.venv/bin/python" python3 python; do
    if command -v "$python_bin" >/dev/null 2>&1; then
      break
    fi
    python_bin=""
  done
  [[ -n "$python_bin" ]] || {
    echo "ERROR: Python is required to validate the dependency cache." >&2
    return 1
  }
  args=(
    "$ROOT/verification/lifecycle.py" artifact-path
    --manifest "$ARTIFACT_MANIFEST" --cache "$DEPENDENCY_CACHE"
    --artifact-id "$artifact_id"
    --expected-requested-version "$expected_version"
    --root "$ROOT" --reproducible
  )
  if [[ "$expected_version" != "dynamic" ]]; then
    args+=(--expected-version "$expected_version")
  fi
  "$python_bin" "${args[@]}"
}

atomic_from_stdin() {
  local target="$1" temporary
  mkdir -p "$(dirname "$target")"
  temporary="$(mktemp "${target}.tmp.XXXXXX")"
  if ! cat > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  if ! mv -f "$temporary" "$target"; then
    rm -f "$temporary"
    return 1
  fi
}

register_ide_ownership() {
  local python_bin
  for python_bin in "$ROOT/env/.venv/bin/python" python3 python; do
    command -v "$python_bin" >/dev/null 2>&1 && break
    python_bin=""
  done
  [[ -n "$python_bin" ]] || {
    echo "ERROR: Python is required to register IDE ownership" >&2
    return 1
  }
  "$python_bin" "$ROOT/verification/lifecycle.py" state init \
    --root "$ROOT" --home "$HOME" >/dev/null
  "$python_bin" "$ROOT/verification/lifecycle.py" state own-tree \
    --root "$ROOT" --home "$HOME" --path "$VSCODIUM_ROOT" >/dev/null
  "$python_bin" "$ROOT/verification/lifecycle.py" state own-tree \
    --root "$ROOT" --home "$HOME" --path "$ROOT/state/generated" >/dev/null
}

install_codium_from_cache() {
  local archive stage app mount artifact_id
  if [[ "$(uname -m)" == "x86_64" ]]; then
    artifact_id="vscodium-darwin-x64"
  else
    artifact_id="vscodium-darwin-arm64"
  fi
  archive="$(artifact_path "$artifact_id" "$VSCODIUM_VERSION")"
  stage="$(mktemp -d)"
  case "$archive" in
    *.zip)
      ditto -x -k "$archive" "$stage"
      app="$(find "$stage" -maxdepth 3 -name 'VSCodium.app' -type d | head -1)"
      ;;
    *.dmg)
      mount="$(hdiutil attach -nobrowse -readonly "$archive" |
        awk '/Apple_HFS|Apple_APFS/ {print $NF; exit}')"
      [[ -n "$mount" ]] || { rm -rf "$stage"; return 1; }
      app="$mount/VSCodium.app"
      ;;
    *)
      rm -rf "$stage"
      echo "ERROR: validated VSCodium export must be a .zip or .dmg" >&2
      return 1
      ;;
  esac
  [[ -d "$app" ]] || {
    [[ -z "${mount:-}" ]] || hdiutil detach "$mount" >/dev/null
    rm -rf "$stage"
    echo "ERROR: VSCodium.app is absent from the validated export" >&2
    return 1
  }
  rm -rf "$VSCODIUM_APP"
  mkdir -p "$VSCODIUM_ROOT"
  ditto "$app" "$VSCODIUM_APP"
  [[ -z "${mount:-}" ]] || hdiutil detach "$mount" >/dev/null
  rm -rf "$stage"
}

install_oracle_agents_extension() {
  # The agents sidebar is a local extension shipped with the repo. It MUST be
  # packed as a .vsix and installed via the codium CLI - a folder copied into
  # .vscode-oss/extensions is ignored (extensions.json is the registry).
  local src="$ROOT/connectors/ide/oracle-agents"
  [[ -f "$src/package.json" ]] || {
    echo "ERROR: bundled Oracle agents extension source is missing" >&2
    return 1
  }
  local ver stage vsix
  ver="$(jq -er .version "$src/package.json")"
  stage="$(mktemp -d)"
  mkdir -p "$stage/extension/media" "$VSIX_DIR"
  sed "s/Id=\"oracle-agents\" Version=\"[^\"]*\"/Id=\"oracle-agents\" Version=\"$ver\"/" \
    "$src/extension.vsixmanifest" > "$stage/extension.vsixmanifest"
  cat > "$stage/[Content_Types].xml" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Default Extension="css" ContentType="text/css"/>
  <Default Extension="md" ContentType="text/markdown"/>
</Types>
EOF
  cp "$src/package.json" "$src/extension.js" "$stage/extension/"
  cp -R "$src/media/." "$stage/extension/media/"
  vsix="$VSIX_DIR/sentivue.oracle-agents-$ver.vsix"
  rm -f "$vsix"
  (cd "$stage" && zip -qr "$vsix" "[Content_Types].xml" extension.vsixmanifest extension)
  rm -rf "$stage"
  "$CODIUM_BIN" --user-data-dir "$USER_DATA_DIR" \
    --extensions-dir "$EXTENSIONS_DIR" --install-extension "$vsix" --force >/dev/null
  echo "==> installed agents sidebar extension (sentivue.oracle-agents-$ver)"
}

graft_ripgrep() {
  # Open VSX builds of Continue ship without the ripgrep binary and die on
  # activation with "Could not find ripgrep binary" - graft VSCodium's own rg in.
  local ext rg dest
  ext="$(ls -d "$EXTENSIONS_DIR"/continue.continue-* 2>/dev/null | sort | tail -1)"
  [[ -n "$ext" ]] || {
    echo "ERROR: installed Continue extension is missing" >&2
    return 1
  }
  dest="$ext/out/node_modules/@vscode/ripgrep/bin"
  [[ -x "$dest/rg" ]] && return 0
  rg="$(find "$VSCODIUM_APP/Contents/Resources/app/node_modules/@vscode" -name rg -type f 2>/dev/null | head -1)"
  [[ -n "$rg" ]] || rg="$(command -v rg || true)"
  [[ -n "$rg" ]] || {
    echo "ERROR: no policy-bound ripgrep binary is available for Continue" >&2
    return 1
  }
  mkdir -p "$dest"
  cp "$rg" "$dest/rg" && chmod +x "$dest/rg"
  echo "==> grafted ripgrep into Continue (Open VSX build ships without it)"
}

update_user_config() {
  # This dedicated user-data directory is Oracle-owned; canonical user settings
  # remain untouched.
  local dir="$USER_DATA_DIR/User"
  mkdir -p "$dir"
  local settings="$dir/settings.json"
  [[ -f "$settings" ]] || echo '{}' > "$settings"
  if ! command -v jq >/dev/null; then
    echo "ERROR: jq is required to write Oracle VSCodium settings" >&2
    return 1
  fi
  local tab="$ROOT/connectors/ide/agent-tab.sh"
  local merged
  if ! merged="$(jq \
    --arg tab "$tab" \
    '. + {
      "telemetry.telemetryLevel": "off",
      "update.mode": "none",
      "extensions.autoUpdate": false,
      "extensions.autoCheckUpdates": false,
      "editor.inlineSuggest.enabled": true,
      "continue.enableTabAutocomplete": true,
      "workbench.colorTheme": (."workbench.colorTheme" // "Default Dark Modern"),
      "terminal.integrated.profiles.osx": ((."terminal.integrated.profiles.osx" // {}) + {
        "Oracle Agent: Claude Code": {
          "path": "/bin/bash", "args": [$tab, "claude"],
          "icon": "hubot", "color": "terminal.ansiCyan", "overrideName": true
        },
        "Oracle Agent: Claude Code (worktree)": {
          "path": "/bin/bash", "args": [$tab, "claude", "--worktree"],
          "icon": "git-branch", "color": "terminal.ansiBlue", "overrideName": true
        },
        "Oracle Agent: OpenCode": {
          "path": "/bin/bash", "args": [$tab, "opencode"],
          "icon": "rocket", "color": "terminal.ansiMagenta", "overrideName": true
        },
        "Oracle Agent: Kilo Code": {
          "path": "/bin/bash", "args": [$tab, "kilo"],
          "icon": "circuit-board", "color": "terminal.ansiYellow", "overrideName": true
        }
      })
    }' "$settings")"; then
    echo "ERROR: Oracle VSCodium settings are malformed" >&2
    return 1
  fi
  printf '%s\n' "$merged" | atomic_from_stdin "$settings"
  echo "==> merged VSCodium user settings (agent-tab profiles, telemetry off)"

  # Keybindings for new agent tabs (created only if the user has none yet)
  local keys="$dir/keybindings.json"
  if [[ -f "$keys" ]] && grep -q "Oracle Agent: Claude Code" "$keys" && grep -q '"location": "view"' "$keys"; then
    # normalize our own earlier defaults back to editor tabs
    sed 's/"location": "view"/"location": "editor"/g' "$keys" |
      atomic_from_stdin "$keys"
    echo "==> keybindings normalized: agent tabs open as editor tabs"
  fi
  if [[ ! -f "$keys" ]]; then
    atomic_from_stdin "$keys" <<'EOF'
[
  {
    "key": "cmd+shift+a",
    "command": "workbench.action.terminal.newWithProfile",
    "args": { "profileName": "Oracle Agent: Claude Code", "location": "editor" }
  },
  {
    "key": "cmd+shift+alt+a",
    "command": "workbench.action.terminal.newWithProfile",
    "args": { "profileName": "Oracle Agent: Claude Code (worktree)", "location": "editor" }
  },
  {
    "key": "cmd+alt+o",
    "command": "workbench.action.terminal.newWithProfile",
    "args": { "profileName": "Oracle Agent: OpenCode", "location": "editor" }
  }
]
EOF
    echo "==> wrote keybindings: Cmd+Shift+A agent tab, Cmd+Shift+Alt+A worktree, Cmd+Alt+O opencode"
  fi
}

case "${1:-launch}" in
  install)
    echo "==> installing VSCodium from policy-bound offline export"
    install_codium_from_cache
    [[ -x "$CODIUM_BIN" ]] || {
      echo "ERROR: cached VSCodium install did not provide the codium CLI" >&2
      exit 1
    }
    mkdir -p "$VSIX_DIR"
    if [[ "$(uname -m)" == "x86_64" ]]; then
      continue_id="continue-vsix-darwin-x64"
      kilo_id="kilo-vsix-darwin-x64"
    else
      continue_id="continue-vsix-darwin-arm64"
      kilo_id="kilo-vsix-darwin-arm64"
    fi
    # migration: Roo Code was discontinued (May 2026) - replace it with Kilo
    "$CODIUM_BIN" --user-data-dir "$USER_DATA_DIR" \
      --extensions-dir "$EXTENSIONS_DIR" \
      --uninstall-extension RooVeterinaryInc.roo-cline >/dev/null 2>&1 || true
    cont="$(artifact_path "$continue_id" "$CONTINUE_VSIX_VERSION")"
    kilo="$(artifact_path "$kilo_id" "$KILO_VSIX_VERSION")"
    "$CODIUM_BIN" --user-data-dir "$USER_DATA_DIR" \
      --extensions-dir "$EXTENSIONS_DIR" --install-extension "$cont" --force
    "$CODIUM_BIN" --user-data-dir "$USER_DATA_DIR" \
      --extensions-dir "$EXTENSIONS_DIR" --install-extension "$kilo" --force
    install_oracle_agents_extension
    graft_ripgrep
    bash "$ROOT/connectors/ide/sync-models.sh"
    update_user_config
    register_ide_ownership
    echo
    echo "IDE ready. Models are auto-detected on every launch; Kilo Code reads"
    echo "its generated config from state/generated/kilo/kilo.jsonc (local provider only)."
    echo "Agent tabs: Cmd+Shift+A (Claude Code), Cmd+Shift+Alt+A (worktree),"
    echo "Cmd+Alt+O (OpenCode) - or the terminal '+' dropdown, 'Oracle Agent' profiles"
    echo "(Claude Code, OpenCode, Kilo Code)."
    echo "Launch with: oracle ide"
    ;;
  sync)
    exec bash "$ROOT/connectors/ide/sync-models.sh"
    ;;
  launch)
    [[ -x "$CODIUM_BIN" ]] || { echo "IDE not installed - run: bash connectors/ide/setup-ide.sh install"; exit 1; }
    # Refresh model detection + profile paths before selecting generated configs.
    bash "$ROOT/connectors/ide/sync-models.sh"
    export CONTINUE_GLOBAL_DIR="$ROOT/state/generated/continue"
    export KILO_CONFIG="$ROOT/state/generated/kilo/kilo.jsonc"
    export OPENCODE_CONFIG="$KILO_CONFIG"
    update_user_config
    exec "$CODIUM_BIN" --user-data-dir "$USER_DATA_DIR" \
      --extensions-dir "$EXTENSIONS_DIR" "$ROOT"
    ;;
  *) echo "usage: setup-ide.sh {install|sync|launch}"; exit 1 ;;
esac
