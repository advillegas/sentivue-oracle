# Windows twin of setup-ide.sh: the Cursor-like IDE on local models.
# VSCodium (telemetry-free VS Code) + Continue (chat/edit/autocomplete) +
# Kilo Code (agentic panel), all pointed at llama-swap on 127.0.0.1:9099.
# (Kilo Code replaced Roo Code, which was discontinued May 2026.)
# Parallel agent tabs: Ctrl+Shift+A opens an engine session as an editor tab,
# any number at once, each optionally in its own git worktree (no collisions).
# Models are auto-detected from the machine on every launch (sync-models.ps1).
#
#   powershell -File connectors\ide\setup-ide.ps1 install
#   powershell -File connectors\ide\setup-ide.ps1 sync
#   powershell -File connectors\ide\setup-ide.ps1 launch
param([Parameter(Position = 0)][string]$Cmd = "launch")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Find-Codium {
    $c = Get-Command codium -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\VSCodium\bin\codium.cmd",
        "$env:ProgramFiles\VSCodium\bin\codium.cmd"
    )
    foreach ($p in $candidates) { if (Test-Path $p) { return $p } }
    return $null
}

function Install-OracleAgentsExtension {
    # The agents sidebar is a local extension shipped with the repo. It MUST be
    # packed as a .vsix and installed via the codium CLI - a folder copied into
    # .vscode-oss\extensions is ignored (extensions.json is the registry).
    $src = Join-Path $PSScriptRoot "oracle-agents"
    if (-not (Test-Path (Join-Path $src "package.json"))) { return }
    $codium = Find-Codium
    if (-not $codium) { return }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "pack-extension.ps1")
    $ver = (Get-Content (Join-Path $src "package.json") -Raw | ConvertFrom-Json).version
    $vsix = Join-Path $Root "incoming\vsix\sentivue.oracle-agents-$ver.vsix"
    if (Test-Path $vsix) {
        & $codium --install-extension $vsix --force | Out-Null
        Write-Host "==> installed agents sidebar extension (sentivue.oracle-agents-$ver)"
    }
}

function Update-UserConfig {
    # Merge (never clobber) the Oracle keys into VSCodium user settings:
    # telemetry off, Roo auto-import path, and the agent-tab terminal profiles.
    param([switch]$Quiet)
    $userDir = Join-Path $env:APPDATA "VSCodium\User"
    New-Item -ItemType Directory -Force -Path $userDir | Out-Null
    $settingsPath = Join-Path $userDir "settings.json"
    $settings = @{}
    if (Test-Path $settingsPath) {
        try {
            $obj = Get-Content $settingsPath -Raw | ConvertFrom-Json
            foreach ($p in $obj.PSObject.Properties) { $settings[$p.Name] = $p.Value }
        } catch {
            if (-not $Quiet) { Write-Host "WARN: could not parse existing settings.json - leaving it untouched" }
            return
        }
    }
    $agentTab = Join-Path $PSScriptRoot "agent-tab.ps1"
    $settings["telemetry.telemetryLevel"] = "off"   # Kilo Code honors this too
    $settings["update.mode"] = "none"
    $settings["extensions.autoUpdate"] = $false
    $settings["extensions.autoCheckUpdates"] = $false
    $settings["editor.inlineSuggest.enabled"] = $true
    $settings["continue.enableTabAutocomplete"] = $true
    $settings.Remove("roo-cline.autoImportSettingsPath") | Out-Null   # Roo retired
    if (-not $settings.ContainsKey("workbench.colorTheme")) { $settings["workbench.colorTheme"] = "Default Dark Modern" }
    $profiles = @{}
    if ($settings.ContainsKey("terminal.integrated.profiles.windows") -and $settings["terminal.integrated.profiles.windows"]) {
        foreach ($p in $settings["terminal.integrated.profiles.windows"].PSObject.Properties) { $profiles[$p.Name] = $p.Value }
    }
    $profiles["Oracle Agent: Claude Code"] = @{
        path = "powershell.exe"
        args = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $agentTab, "claude")
        icon = "hubot"; color = "terminal.ansiCyan"; overrideName = $true
    }
    $profiles["Oracle Agent: Claude Code (worktree)"] = @{
        path = "powershell.exe"
        args = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $agentTab, "claude", "-Worktree")
        icon = "git-branch"; color = "terminal.ansiBlue"; overrideName = $true
    }
    $profiles["Oracle Agent: OpenCode"] = @{
        path = "powershell.exe"
        args = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $agentTab, "opencode")
        icon = "rocket"; color = "terminal.ansiMagenta"; overrideName = $true
    }
    $profiles["Oracle Agent: Kilo Code"] = @{
        path = "powershell.exe"
        args = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $agentTab, "kilo")
        icon = "circuit-board"; color = "terminal.ansiYellow"; overrideName = $true
    }
    $settings["terminal.integrated.profiles.windows"] = $profiles
    ConvertTo-Json -InputObject $settings -Depth 20 | Set-Content -Path $settingsPath
    if (-not $Quiet) { Write-Host "==> merged VSCodium user settings (agent-tab profiles, telemetry off)" }

    # Keybindings for new agent tabs (created only if the user has none yet).
    # Agents open as EDITOR TABS (any number side by side); the Agents sidebar
    # in the secondary side bar is the switchboard that lists and focuses them.
    $keysPath = Join-Path $userDir "keybindings.json"
    if (Test-Path $keysPath) {
        # normalize our own earlier defaults back to editor tabs
        $raw = Get-Content $keysPath -Raw
        if ($raw -match "Oracle Agent: Claude Code" -and $raw -match '"location": "view"') {
            $raw -replace '"location": "view"', '"location": "editor"' | Set-Content $keysPath
            if (-not $Quiet) { Write-Host "==> keybindings normalized: agent tabs open as editor tabs" }
        }
    }
    if (-not (Test-Path $keysPath)) {
        @'
[
  {
    "key": "ctrl+shift+a",
    "command": "workbench.action.terminal.newWithProfile",
    "args": { "profileName": "Oracle Agent: Claude Code", "location": "editor" }
  },
  {
    "key": "ctrl+shift+alt+a",
    "command": "workbench.action.terminal.newWithProfile",
    "args": { "profileName": "Oracle Agent: Claude Code (worktree)", "location": "editor" }
  },
  {
    "key": "ctrl+alt+o",
    "command": "workbench.action.terminal.newWithProfile",
    "args": { "profileName": "Oracle Agent: OpenCode", "location": "editor" }
  }
]
'@ | Set-Content -Path $keysPath
        if (-not $Quiet) { Write-Host "==> wrote keybindings: Ctrl+Shift+A agent tab, Ctrl+Shift+Alt+A worktree, Ctrl+Alt+O opencode" }
    }
}

switch ($Cmd) {
    "install" {
        $codium = Find-Codium
        if (-not $codium) {
            Write-Host "==> installing VSCodium (winget)"
            winget install --id VSCodium.VSCodium -e --silent --accept-package-agreements --accept-source-agreements
            $codium = Find-Codium
            if (-not $codium) { Write-Host "ERROR: VSCodium installed but codium CLI not found - reopen the terminal and re-run"; exit 1 }
        }
        $vsix = Join-Path $Root "incoming\vsix"
        New-Item -ItemType Directory -Force -Path $vsix | Out-Null
        # migration: Roo Code was discontinued (May 2026) - replace it with Kilo
        & $codium --uninstall-extension RooVeterinaryInc.roo-cline 2>$null | Out-Null
        # Continue ships native modules (sqlite3, lancedb, onnx) - must take the
        # win32-x64 build, not the universal one, or activation fails.
        foreach ($ext in @(@("Continue", "continue", "win32-x64"), @("kilocode", "kilo-code", "win32-x64"))) {
            $ns = $ext[0]; $name = $ext[1]; $plat = $ext[2]
            $meta = $null
            if ($plat) {
                try { $meta = Invoke-RestMethod -Uri "https://open-vsx.org/api/$ns/$name/$plat/latest" } catch { $meta = $null }
            }
            if (-not $meta) { $meta = Invoke-RestMethod -Uri "https://open-vsx.org/api/$ns/$name/latest" }
            $tag = if ($plat) { "$plat-" } else { "" }
            $file = Join-Path $vsix "$ns.$name-$tag$($meta.version).vsix"
            Write-Host "==> $ns.$name $($meta.version) $plat"
            if (-not (Test-Path $file)) { Invoke-WebRequest -Uri $meta.files.download -OutFile $file }
            & $codium --install-extension $file --force | Out-Null
        }
        Install-OracleAgentsExtension
        # Open VSX builds of Continue ship WITHOUT the ripgrep binary, so the
        # extension dies on activation with "Could not find ripgrep binary".
        # Graft VSCodium's own rg.exe into the extension to fix it.
        $contExt = Get-ChildItem "$env:USERPROFILE\.vscode-oss\extensions" -Directory -ErrorAction SilentlyContinue |
            Where-Object Name -match "^continue\.continue-" | Sort-Object Name -Descending | Select-Object -First 1
        if ($contExt) {
            $rgDest = Join-Path $contExt.FullName "out\node_modules\@vscode\ripgrep\bin"
            if (-not (Test-Path (Join-Path $rgDest "rg.exe"))) {
                $codiumRoot = Split-Path -Parent (Split-Path -Parent $codium)  # ...\VSCodium from ...\VSCodium\bin\codium.cmd
                $rgSrc = Get-ChildItem (Join-Path $codiumRoot "resources\app\node_modules\@vscode") -Recurse -Filter "rg.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($rgSrc) {
                    New-Item -ItemType Directory -Force -Path $rgDest | Out-Null
                    Copy-Item $rgSrc.FullName (Join-Path $rgDest "rg.exe") -Force
                    Write-Host "==> grafted ripgrep into Continue (Open VSX build ships without it)"
                }
            }
        }
        # Continue + Kilo model config: auto-detected from this machine
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync-models.ps1")
        Update-UserConfig
        Write-Host ""
        Write-Host "IDE ready. Models are auto-detected on every launch; Kilo Code reads"
        Write-Host "its generated config from ~\.config\kilo\kilo.jsonc (local provider only)."
        Write-Host "Agent tabs: Ctrl+Shift+A (Claude Code), Ctrl+Shift+Alt+A (worktree),"
        Write-Host "Ctrl+Alt+O (OpenCode) - or the terminal '+' dropdown, 'Oracle Agent' profiles"
        Write-Host "(Claude Code, OpenCode, Kilo Code)."
        Write-Host "Launch with: bin\oracle.ps1 ide"
    }
    "sync" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync-models.ps1")
    }
    "launch" {
        $codium = Find-Codium
        if (-not $codium) { Write-Host "IDE not installed - run: bin\oracle.ps1 ide install"; exit 1 }
        # refresh model detection + profile paths on every launch (best effort)
        try { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync-models.ps1") } catch {}
        try { Update-UserConfig -Quiet } catch {}
        & $codium $Root
    }
    default { Write-Host "usage: setup-ide.ps1 {install|sync|launch}" }
}
