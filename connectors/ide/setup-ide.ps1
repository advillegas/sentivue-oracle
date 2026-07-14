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
$env:ORACLE_ROOT = $Root
$env:ORACLE_PROJECT_ROOT = $Root
. (Join-Path $Root "engines\shared\lean-ctx-env.ps1")
$env:PATH = (Join-Path $Root "env\.venv\Scripts") + ";" +
    (Join-Path $Root ".tools\bin") + ";" + $env:PATH
$DependencyCache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
$ArtifactManifest = Join-Path $DependencyCache "manifest.json"
$VSCodiumRoot = Join-Path $Root ".tools\vscodium"
$VSCodiumAppRoot = Join-Path $VSCodiumRoot "app"
$ExtensionsDir = Join-Path $VSCodiumRoot "extensions"
$UserDataDir = Join-Path $Root "state\generated\vscodium"
$VsixDir = Join-Path $VSCodiumRoot "vsix"

function Get-LockedVersion([string]$Name) {
    $line = Get-Content (Join-Path $Root "VERSIONS.lock") |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -First 1
    if (-not $line) { throw "missing $Name in VERSIONS.lock" }
    return ((($line -split "=", 2)[1]) -split "#", 2)[0].Trim()
}

function Get-CachedArtifact([string]$Id, [string]$Version) {
    $python = Join-Path $Root "env\.venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    $lifecycleArgs = @(
        (Join-Path $Root "verification\lifecycle.py"), "artifact-path",
        "--manifest", $ArtifactManifest, "--cache", $DependencyCache,
        "--artifact-id", $Id,
        "--expected-requested-version", $Version,
        "--root", $Root, "--reproducible"
    )
    if ($Version -ne "dynamic") {
        $lifecycleArgs += @("--expected-version", $Version)
    }
    $path = (& $python @lifecycleArgs)
    if ($LASTEXITCODE -ne 0) { throw "cached artifact validation failed: $Id" }
    return $path.Trim()
}

function Write-Utf8NoBomAtomic([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent (".{0}.{1}.tmp" -f
        [IO.Path]::GetFileName($Path), [Guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllText(
            $temporary, $Text, (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Find-Codium {
    $candidate = Get-ChildItem $VSCodiumAppRoot -Recurse -Filter "codium.cmd" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { return $candidate.FullName }
    return $null
}

function Install-OracleAgentsExtension {
    # The agents sidebar is a local extension shipped with the repo. It MUST be
    # packed as a .vsix and installed via the codium CLI - a folder copied into
    # .vscode-oss\extensions is ignored (extensions.json is the registry).
    $src = Join-Path $PSScriptRoot "oracle-agents"
    if (-not (Test-Path (Join-Path $src "package.json"))) {
        throw "bundled Oracle agents extension source is missing"
    }
    $codium = Find-Codium
    if (-not $codium) { throw "repo-local VSCodium is missing" }
    & powershell -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "pack-extension.ps1") -OutDir $VsixDir
    if ($LASTEXITCODE -ne 0) { throw "Oracle agents extension packaging failed" }
    $ver = (Get-Content (Join-Path $src "package.json") -Raw | ConvertFrom-Json).version
    $vsix = Join-Path $VsixDir "sentivue.oracle-agents-$ver.vsix"
    if (-not (Test-Path $vsix)) { throw "packed Oracle agents VSIX is missing" }
    & $codium --user-data-dir $UserDataDir --extensions-dir $ExtensionsDir `
        --install-extension $vsix --force | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Oracle agents extension install failed" }
    Write-Host "==> installed agents sidebar extension (sentivue.oracle-agents-$ver)"
}

function Install-DesktopShortcut {
    # Double-clickable Desktop launcher for the IDE; ownership-registered so
    # uninstall removes it. Existing user shortcuts are replaced atomically.
    $codium = Find-Codium
    if (-not $codium) { throw "repo-local VSCodium is missing" }
    $desktop = [Environment]::GetFolderPath("Desktop")
    New-Item -ItemType Directory -Force -Path $desktop | Out-Null
    $shortcutPath = Join-Path $desktop "SentiVue Oracle.lnk"
    $temporary = Join-Path $desktop (".sentivue-shortcut-{0}.lnk" -f [Guid]::NewGuid().ToString("N"))
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($temporary)
        $link.TargetPath = "powershell.exe"
        $link.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" launch' -f
            (Join-Path $PSScriptRoot "setup-ide.ps1"))
        $codiumExe = Get-ChildItem $VSCodiumAppRoot -Recurse -Filter "VSCodium.exe" `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($codiumExe) { $link.IconLocation = "$($codiumExe.FullName),0" }
        $link.WorkingDirectory = $Root
        $link.Description = "SentiVue Oracle - your own Cursor on local models"
        $link.Save()
        Move-Item -LiteralPath $temporary -Destination $shortcutPath -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    Write-Host "==> desktop shortcut: SentiVue Oracle -> your IDE"
    return $shortcutPath
}

function Update-UserConfig {
    # This dedicated user-data directory is Oracle-owned; canonical user files
    # remain untouched.
    param([switch]$Quiet)
    $userDir = Join-Path $UserDataDir "User"
    New-Item -ItemType Directory -Force -Path $userDir | Out-Null
    $settingsPath = Join-Path $userDir "settings.json"
    $settings = @{}
    if (Test-Path $settingsPath) {
        try {
            $obj = Get-Content $settingsPath -Raw | ConvertFrom-Json
            foreach ($p in $obj.PSObject.Properties) { $settings[$p.Name] = $p.Value }
        } catch {
            throw "Oracle VSCodium settings are malformed"
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
    $settingsJson = ConvertTo-Json -InputObject $settings -Depth 20
    Write-Utf8NoBomAtomic $settingsPath ($settingsJson + "`n")
    if (-not $Quiet) { Write-Host "==> merged VSCodium user settings (agent-tab profiles, telemetry off)" }

    # Keybindings for new agent tabs (created only if the user has none yet).
    # Agents open as EDITOR TABS (any number side by side); the Agents sidebar
    # in the secondary side bar is the switchboard that lists and focuses them.
    $keysPath = Join-Path $userDir "keybindings.json"
    if (Test-Path $keysPath) {
        # normalize our own earlier defaults back to editor tabs
        $raw = Get-Content $keysPath -Raw
        if ($raw -match "Oracle Agent: Claude Code" -and $raw -match '"location": "view"') {
            $normalized = $raw -replace '"location": "view"', '"location": "editor"'
            Write-Utf8NoBomAtomic $keysPath $normalized
            if (-not $Quiet) { Write-Host "==> keybindings normalized: agent tabs open as editor tabs" }
        }
    }
    if (-not (Test-Path $keysPath)) {
        $keybindings = @'
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
'@
        Write-Utf8NoBomAtomic $keysPath ($keybindings + "`n")
        if (-not $Quiet) { Write-Host "==> wrote keybindings: Ctrl+Shift+A agent tab, Ctrl+Shift+Alt+A worktree, Ctrl+Alt+O opencode" }
    }
}

switch ($Cmd) {
    "install" {
        Write-Host "==> installing VSCodium from policy-bound offline export"
        $archive = Get-CachedArtifact "vscodium-windows-x64" (Get-LockedVersion "VSCODIUM_VERSION")
        if ([IO.Path]::GetExtension($archive) -ne ".zip") {
            throw "validated Windows VSCodium export must be a portable .zip"
        }
        New-Item -ItemType Directory -Force -Path $VSCodiumRoot | Out-Null
        $stage = Join-Path $VSCodiumRoot (".app-stage-" + [Guid]::NewGuid().ToString("N"))
        $newApp = Join-Path $VSCodiumRoot "app.new"
        try {
            Expand-Archive -LiteralPath $archive -DestinationPath $stage -Force
            $stagedCodium = Get-ChildItem $stage -Recurse -Filter "codium.cmd" |
                Select-Object -First 1
            if (-not $stagedCodium) { throw "cached VSCodium archive has no codium.cmd" }
            $stagedApp = Split-Path -Parent (Split-Path -Parent $stagedCodium.FullName)
            Remove-Item -LiteralPath $newApp -Recurse -Force -ErrorAction SilentlyContinue
            Copy-Item -LiteralPath $stagedApp -Destination $newApp -Recurse -Force
            Remove-Item -LiteralPath $VSCodiumAppRoot -Recurse -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $newApp -Destination $VSCodiumAppRoot
        } finally {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $newApp -Recurse -Force -ErrorAction SilentlyContinue
        }
        $codium = Find-Codium
        if (-not $codium) { throw "cached VSCodium install did not provide the codium CLI" }
        New-Item -ItemType Directory -Force -Path $VsixDir, $ExtensionsDir | Out-Null
        # migration: Roo Code was discontinued (May 2026) - replace it with Kilo
        & $codium --user-data-dir $UserDataDir --extensions-dir $ExtensionsDir `
            --uninstall-extension RooVeterinaryInc.roo-cline 2>$null | Out-Null
        # Native extensions are pre-exported for win32-x64 and hash-verified.
        foreach ($ext in @(
            @("continue-vsix-windows-x64", "CONTINUE_VSIX_VERSION"),
            @("kilo-vsix-windows-x64", "KILO_VSIX_VERSION")
        )) {
            $version = Get-LockedVersion $ext[1]
            $file = Get-CachedArtifact $ext[0] $version
            Write-Host "==> $($ext[0]) $version"
            & $codium --user-data-dir $UserDataDir --extensions-dir $ExtensionsDir `
                --install-extension $file --force | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "$($ext[0]) installation failed" }
        }
        Install-OracleAgentsExtension
        # Open VSX builds of Continue ship WITHOUT the ripgrep binary, so the
        # extension dies on activation with "Could not find ripgrep binary".
        # Graft VSCodium's own rg.exe into the extension to fix it.
        $contExt = Get-ChildItem $ExtensionsDir -Directory -ErrorAction SilentlyContinue |
            Where-Object Name -match "^continue\.continue-" | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $contExt) { throw "installed Continue extension is missing" }
        $rgDest = Join-Path $contExt.FullName "out\node_modules\@vscode\ripgrep\bin"
        if (-not (Test-Path (Join-Path $rgDest "rg.exe"))) {
            $rgSrc = Get-ChildItem $VSCodiumAppRoot -Recurse -Filter "rg.exe" `
                -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $rgSrc) { throw "VSCodium export has no ripgrep binary for Continue" }
            New-Item -ItemType Directory -Force -Path $rgDest | Out-Null
            Copy-Item $rgSrc.FullName (Join-Path $rgDest "rg.exe") -Force
            Write-Host "==> grafted ripgrep into Continue (Open VSX build ships without it)"
        }
        # Continue + Kilo model config: auto-detected from this machine
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync-models.ps1")
        if ($LASTEXITCODE -ne 0) { throw "generated model configuration failed" }
        Update-UserConfig
        $shortcut = Install-DesktopShortcut
        $python = Join-Path $Root "env\.venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $python)) {
            $python = (Get-Command python -ErrorAction Stop).Source
        }
        $lifecycle = Join-Path $Root "verification\lifecycle.py"
        & $python $lifecycle state init --root $Root --home $env:USERPROFILE | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "install state initialization failed" }
        foreach ($ownedTree in @($VSCodiumRoot, (Join-Path $Root "state\generated"))) {
            & $python $lifecycle state own-tree --root $Root --home $env:USERPROFILE `
                --path $ownedTree | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "IDE ownership registration failed" }
        }
        & $python $lifecycle state own --root $Root --home $env:USERPROFILE `
            --path $shortcut | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "desktop shortcut ownership registration failed" }
        Write-Host ""
        Write-Host "IDE ready. Models are auto-detected on every launch; Kilo Code reads"
        Write-Host "its generated config from state\generated\kilo\kilo.jsonc (local provider only)."
        Write-Host "Agent tabs: Ctrl+Shift+A (Claude Code), Ctrl+Shift+Alt+A (worktree),"
        Write-Host "Ctrl+Alt+O (OpenCode) - or the terminal '+' dropdown, 'Oracle Agent' profiles"
        Write-Host "(Claude Code, OpenCode, Kilo Code)."
        Write-Host "Launch with: bin\oracle.ps1 ide"
    }
    "sync" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync-models.ps1")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    "launch" {
        $codium = Find-Codium
        if (-not $codium) { Write-Host "IDE not installed - run: bin\oracle.ps1 ide install"; exit 1 }
        # Refresh model detection + profile paths before selecting generated configs.
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "sync-models.ps1")
        if ($LASTEXITCODE -ne 0) { throw "generated model configuration failed" }
        $env:CONTINUE_GLOBAL_DIR = Join-Path $Root "state\generated\continue"
        $env:KILO_CONFIG = Join-Path $Root "state\generated\kilo\kilo.jsonc"
        $env:OPENCODE_CONFIG = $env:KILO_CONFIG
        Update-UserConfig -Quiet
        & $codium --user-data-dir $UserDataDir --extensions-dir $ExtensionsDir $Root
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    default { Write-Host "usage: setup-ide.ps1 {install|sync|launch}" }
}
