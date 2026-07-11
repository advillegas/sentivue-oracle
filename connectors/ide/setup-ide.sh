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
VSIX_DIR="$ROOT/incoming/vsix"

fetch_vsix() {  # fetch_vsix <namespace> <name> [target-platform]
  # Extensions with native modules (Continue: sqlite3/lancedb/onnx) must use the
  # platform build, not the universal one, or they fail to activate.
  local meta url ver plat="${3:-}"
  if [[ -n "$plat" ]]; then
    meta="$(curl -sf --proto '=https' "https://open-vsx.org/api/$1/$2/$plat/latest" || true)"
  fi
  [[ -n "${meta:-}" ]] || meta="$(curl -sf --proto '=https' "https://open-vsx.org/api/$1/$2/latest")"
  url="$(echo "$meta" | jq -r '.files.download')"
  ver="$(echo "$meta" | jq -r '.version')"
  echo "==> $1.$2 $ver ${plat:-universal}" >&2
  curl -sfL --proto '=https' -o "$VSIX_DIR/$1.$2-$ver.vsix" "$url"
  echo "$VSIX_DIR/$1.$2-$ver.vsix"
}

install_oracle_agents_extension() {
  # The agents sidebar is a local extension shipped with the repo. It MUST be
  # packed as a .vsix and installed via the codium CLI - a folder copied into
  # .vscode-oss/extensions is ignored (extensions.json is the registry).
  local src="$ROOT/connectors/ide/oracle-agents"
  [[ -f "$src/package.json" ]] || return 0
  local ver stage vsix
  ver="$(jq -r .version "$src/package.json" 2>/dev/null || echo 0.2.0)"
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
  codium --install-extension "$vsix" --force >/dev/null
  echo "==> installed agents sidebar extension (sentivue.oracle-agents-$ver)"
}

graft_ripgrep() {
  # Open VSX builds of Continue ship without the ripgrep binary and die on
  # activation with "Could not find ripgrep binary" - graft VSCodium's own rg in.
  local ext rg dest
  ext="$(ls -d "$HOME/.vscode-oss/extensions"/continue.continue-* 2>/dev/null | sort | tail -1)"
  [[ -n "$ext" ]] || return 0
  dest="$ext/out/node_modules/@vscode/ripgrep/bin"
  [[ -x "$dest/rg" ]] && return 0
  rg="$(find "/Applications/VSCodium.app/Contents/Resources/app/node_modules/@vscode" -name rg -type f 2>/dev/null | head -1)"
  [[ -n "$rg" ]] || rg="$(command -v rg || true)"
  [[ -n "$rg" ]] || return 0
  mkdir -p "$dest"
  cp "$rg" "$dest/rg" && chmod +x "$dest/rg"
  echo "==> grafted ripgrep into Continue (Open VSX build ships without it)"
}

update_user_config() {
  # Merge (never clobber) the Oracle keys into VSCodium user settings:
  # telemetry off, Roo auto-import path, and the agent-tab terminal profiles.
  local dir="$HOME/Library/Application Support/VSCodium/User"
  mkdir -p "$dir"
  local settings="$dir/settings.json"
  [[ -f "$settings" ]] || echo '{}' > "$settings"
  if ! command -v jq >/dev/null; then
    echo "WARN: jq not found - skipping user settings merge"
    return 0
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
    echo "WARN: could not parse existing settings.json - leaving it untouched"
    return 0
  fi
  echo "$merged" > "$settings"
  echo "==> merged VSCodium user settings (agent-tab profiles, telemetry off)"

  # Keybindings for new agent tabs (created only if the user has none yet)
  local keys="$dir/keybindings.json"
  if [[ -f "$keys" ]] && grep -q "Oracle Agent: Claude Code" "$keys" && grep -q '"location": "view"' "$keys"; then
    # normalize our own earlier defaults back to editor tabs
    sed -i '' 's/"location": "view"/"location": "editor"/g' "$keys" 2>/dev/null || \
      sed -i 's/"location": "view"/"location": "editor"/g' "$keys"
    echo "==> keybindings normalized: agent tabs open as editor tabs"
  fi
  if [[ ! -f "$keys" ]]; then
    cat > "$keys" <<'EOF'
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
    command -v codium >/dev/null || { echo "==> brew install --cask vscodium"; brew install --cask vscodium; }
    mkdir -p "$VSIX_DIR"
    arch="darwin-arm64"; [[ "$(uname -m)" == "x86_64" ]] && arch="darwin-x64"
    # migration: Roo Code was discontinued (May 2026) - replace it with Kilo
    codium --uninstall-extension RooVeterinaryInc.roo-cline >/dev/null 2>&1 || true
    cont="$(fetch_vsix Continue continue "$arch")"
    kilo="$(fetch_vsix kilocode kilo-code "$arch")"
    codium --install-extension "$cont" --force
    codium --install-extension "$kilo" --force
    install_oracle_agents_extension
    graft_ripgrep
    bash "$ROOT/connectors/ide/sync-models.sh"
    update_user_config
    echo
    echo "IDE ready. Models are auto-detected on every launch; Kilo Code reads"
    echo "its generated config from ~/.config/kilo/kilo.jsonc (local provider only)."
    echo "Agent tabs: Cmd+Shift+A (Claude Code), Cmd+Shift+Alt+A (worktree),"
    echo "Cmd+Alt+O (OpenCode) - or the terminal '+' dropdown, 'Oracle Agent' profiles"
    echo "(Claude Code, OpenCode, Kilo Code)."
    echo "Launch with: oracle ide"
    ;;
  sync)
    exec bash "$ROOT/connectors/ide/sync-models.sh"
    ;;
  launch)
    command -v codium >/dev/null || { echo "IDE not installed — run: bash connectors/ide/setup-ide.sh install"; exit 1; }
    # refresh model detection + profile paths on every launch (best effort)
    bash "$ROOT/connectors/ide/sync-models.sh" || true
    update_user_config || true
    exec codium "$ROOT"
    ;;
  *) echo "usage: setup-ide.sh {install|sync|launch}"; exit 1 ;;
esac
