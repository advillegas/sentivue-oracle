# oracle.ps1 - Windows entry point for the self-contained development ecosystem.
# The Mac appliance uses bin/oracle (bash); this exposes the node-appropriate
# subset on Windows: vault, model pre-downloading, and commit/package/push.
#
#   powershell -File bin\oracle.ps1 vault <init|sync|new|clone|list|backup> [...]
#   powershell -File bin\oracle.ps1 models [-Dest E:\oracle-models] [-Only name]
#   powershell -File bin\oracle.ps1 finish [-SkipPush]     commit + package + push (GitHub) + vault sync
param(
    [Parameter(Position = 0)][string]$Cmd = "help",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Find-CodiumInstalled {
    if (Get-Command codium -ErrorAction SilentlyContinue) { return $true }
    return (Test-Path "$env:LOCALAPPDATA\Programs\VSCodium\bin\codium.cmd") -or
           (Test-Path "$env:ProgramFiles\VSCodium\bin\codium.cmd")
}

function Find-RealPython {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps") { return $cmd.Source }
    $c = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($c) { return $c.FullName }
    return $null
}

function Invoke-Conductor {
    param([string[]]$CondArgs)
    $python = Find-RealPython
    if (-not $python) {
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\ensure-tools.ps1")
        $python = Find-RealPython
        if (-not $python) { Write-Host "ERROR: no Python and self-provisioning failed"; exit 1 }
    }
    $pyDir = Split-Path -Parent $python
    $env:PATH = "$pyDir;$pyDir\Scripts;$env:PATH"
    & $python (Join-Path $Root "conductor\conductor.py") @CondArgs
    exit $LASTEXITCODE
}

switch ($Cmd) {
    "vault"  { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") @Rest }
    "models" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\download-models.ps1") @Rest }
    "finish" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\finish-windows.ps1") @Rest }
    "setup" {
        # Full platform on Windows: engines (pinned npm, repo-local) + serving toolchain.
        $lock = Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" }
        $pins = @{}
        foreach ($l in $lock) { $kv = $l -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim() }
        $env:npm_config_prefix = Join-Path $Root ".tools\npm"
        New-Item -ItemType Directory -Force -Path $env:npm_config_prefix | Out-Null
        Write-Host "==> engines: claude-code@$($pins['CLAUDE_CODE_NPM_VERSION']) + opencode@$($pins['OPENCODE_NPM_VERSION']) + kilo@$($pins['KILO_CLI_NPM_VERSION'])"
        npm install -g "@anthropic-ai/claude-code@$($pins['CLAUDE_CODE_NPM_VERSION'])" "opencode-ai@$($pins['OPENCODE_NPM_VERSION'])" "@kilocode/cli@$($pins['KILO_CLI_NPM_VERSION'])"
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\sync-skills.ps1")
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "harness\skill-packs\install-skill-packs.ps1")
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") setup
        Write-Host "==> setup complete: 'oracle.ps1 serve' then 'oracle.ps1 claude'"
    }
    "serve"  { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") start }
    "stop"   { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") stop }
    "status" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") status }
    "claude"   { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\claude-code\launch.ps1") @Rest }
    "opencode" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\opencode\launch.ps1") @Rest }
    "kilo"     { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\kilo\launch.ps1") @Rest }
    "notes" {
        # Obsidian over the repo: the operator's lens on memory/doctrine/reports
        $exe = "$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe"
        if (-not (Test-Path $exe)) {
            Write-Host "==> installing Obsidian (winget, one time)"
            winget install --id Obsidian.Obsidian -e --silent --accept-package-agreements --accept-source-agreements
        }
        if (Test-Path $exe) { Start-Process $exe "obsidian://open?path=$([uri]::EscapeDataString($Root))" }
        else { Write-Host "Obsidian installed - launch it once from the Start menu, then open this folder as a vault: $Root" }
    }
    "agents-ui" {
        # Agent-MCP orchestration viewer (optional): watch agents/tasks/context live
        $sub = if ($Rest.Count -ge 1) { $Rest[0] } else { "start" }
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "harness\agent-mcp\setup-agent-mcp.ps1") $sub
        if ($sub -eq "start") { Start-Sleep 3; Start-Process "http://127.0.0.1:3847" }
    }
    "loops" {
        # loop-engineering CLIs (pinned in .tools\npm): audit|init|cost|sync
        $env:PATH = (Join-Path $Root ".tools\npm") + ";" + $env:PATH
        $sub = if ($Rest.Count -ge 1) { $Rest[0] } else { "audit" }
        $extra = if ($Rest.Count -gt 1) { $Rest[1..($Rest.Count - 1)] } else { @() }
        switch ($sub) {
            "audit" { & loop-audit $Root @extra }
            "init"  { & loop-init $Root @extra }
            "cost"  { & loop-cost @extra }
            "sync"  { & loop-sync $Root @extra }
            default { Write-Host "usage: oracle.ps1 loops {audit|init|cost|sync} [args]" }
        }
    }
    "mission" {
        # oracle.ps1 mission <mission.toml> [claude|opencode|kilo] [hours]
        if ($Rest.Count -lt 1) { Write-Host "usage: oracle.ps1 mission <mission.toml> [claude|opencode|kilo] [hours]"; exit 1 }
        $engine = if ($Rest.Count -ge 2) { $Rest[1] } else { "claude" }
        $hours = if ($Rest.Count -ge 3) { $Rest[2] } else { "24" }
        Invoke-Conductor @("run", $Rest[0], "--engine", $engine, "--hours", $hours)
    }
    "retro" { Invoke-Conductor @("retro", "--engine", $(if ($Rest.Count -ge 1) { $Rest[0] } else { "claude" })) }
    "state" { Invoke-Conductor @("status") }
    "ide" {
        $sub = "launch"; if ($Rest.Count -gt 0) { $sub = $Rest[0] }
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "connectors\ide\setup-ide.ps1") $sub
    }
    "menu" {
        while ($true) {
            Write-Host ""
            Write-Host "==================== SentiVue Oracle ===================="
            Write-Host "  Windows platform - $Root"
            Write-Host "========================================================="
            Write-Host "  0) desktop app (chat/missions)     8) IDE (Cursor-like, local models)"
            Write-Host "  1) Claude Code session             2) OpenCode session"
            Write-Host "  3) sync repo to local vault        4) vault inventory"
            Write-Host "  5) download models                 6) commit + package + push"
            Write-Host "  7) open repo folder                s) serve models   t) status   q) quit"
            $c = Read-Host "choose"
            switch ($c) {
                "0" { & powershell -ExecutionPolicy Bypass -File $PSCommandPath desk }
                "8" {
                    $ide = Join-Path $Root "connectors\ide\setup-ide.ps1"
                    if (Find-CodiumInstalled) { & powershell -ExecutionPolicy Bypass -File $ide launch }
                    else { & powershell -ExecutionPolicy Bypass -File $ide install; & powershell -ExecutionPolicy Bypass -File $ide launch }
                }
                "s" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") start }
                "t" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") status }
                "1" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\claude-code\launch.ps1") }
                "2" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\opencode\launch.ps1") }
                "3" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") sync }
                "4" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") list }
                "5" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\download-models.ps1") }
                "6" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\finish-windows.ps1") }
                "7" { Start-Process explorer.exe $Root }
                "q" { return }
                default { }
            }
        }
    }
    default {
        Write-Host "oracle (Windows - full platform)"
        Write-Host "  setup                                     engines (pinned) + serving toolchain (one time)"
        Write-Host "  serve | status | stop                     local model serving (llama-swap on :9099)"
        Write-Host "  claude | opencode | kilo                  engine sessions on local models"
        Write-Host "  mission <toml> [engine] [hours]           self-governing mission (conductor loop)"
        Write-Host "  retro | state                             process retrospective | mission state"
        Write-Host "  agents-ui [install|start|stop|status]     orchestration viewer (Agent-MCP, optional)"
        Write-Host "  loops   audit|init|cost|sync              loop-engineering toolkit"
        Write-Host "  notes                                     Obsidian over the repo (memory lens)"
        Write-Host "  ide  [install|sync]                       Cursor-like IDE (agent tabs, auto-detected models)"
        Write-Host "  desk                                      native desktop app (chat/missions/models/vault)"
        Write-Host "  menu                                      interactive menu (the desktop shortcut opens this)"
        Write-Host "  vault   init|sync|new|clone|list|backup   local private git remote"
        Write-Host "  models  [-Dest path] [-Only name]         download models (profile-aware)"
        Write-Host "  finish  [-SkipPush]                       commit + package + push + vault sync"
    }
}

