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
        Write-Host "==> engines: claude-code@$($pins['CLAUDE_CODE_NPM_VERSION']) + opencode@$($pins['OPENCODE_NPM_VERSION'])"
        npm install -g "@anthropic-ai/claude-code@$($pins['CLAUDE_CODE_NPM_VERSION'])" "opencode-ai@$($pins['OPENCODE_NPM_VERSION'])"
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") setup
        Write-Host "==> setup complete: 'oracle.ps1 serve' then 'oracle.ps1 claude'"
    }
    "serve"  { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") start }
    "stop"   { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") stop }
    "status" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") status }
    "claude"   { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\claude-code\launch.ps1") @Rest }
    "opencode" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\opencode\launch.ps1") @Rest }
    "desk" {
        $bin = Join-Path $Root "desk\target\release\oracle-desk.exe"
        if (-not (Test-Path $bin)) {
            if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
                Write-Host "==> installing rust (one time, winget)"
                winget install --id Rustlang.Rustup -e --silent
                $env:PATH = "$env:USERPROFILE\.cargo\bin;" + $env:PATH
            }
            # Rust on Windows needs the MSVC linker (VS Build Tools, C++ workload).
            # If absent, install it unattended via winget (one time, ~2 GB).
            $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
            function Test-Msvc {
                (Test-Path $vswhere) -and
                (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
            }
            if (-not (Test-Msvc)) {
                Write-Host "==> installing Visual Studio Build Tools (C++ workload) - one time, ~2 GB, please wait"
                winget install Microsoft.VisualStudio.2022.BuildTools --accept-package-agreements --accept-source-agreements `
                    --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
            }
            Push-Location (Join-Path $Root "desk")
            if (Test-Msvc) {
                Write-Host "==> building oracle-desk (one time, a few minutes)"
                cargo build --release
            } else {
                Write-Host "==> Build Tools unavailable; trying the GNU toolchain (may need MSYS2 binutils)"
                rustup toolchain install stable-gnu --profile minimal
                $sc = "$env:USERPROFILE\.rustup\toolchains\stable-x86_64-pc-windows-gnu\lib\rustlib\x86_64-pc-windows-gnu\bin\self-contained"
                $env:PATH = "$sc;" + $env:PATH
                cargo +stable-gnu build --release
            }
            Pop-Location
        }
        if (Test-Path $bin) {
            Start-Process $bin -WorkingDirectory $Root
        } else {
            Write-Host "BUILD FAILED - oracle-desk.exe was not produced. Review the errors above;"
            Write-Host "re-run after fixing, or install VS Build Tools (C++ workload) for the msvc path."
        }
    }
    "menu" {
        while ($true) {
            Write-Host ""
            Write-Host "==================== SentiVue Oracle ===================="
            Write-Host "  Windows platform - $Root"
            Write-Host "========================================================="
            Write-Host "  0) desktop app (chat/missions)     s) serve models   t) status"
            Write-Host "  1) Claude Code session             2) OpenCode session"
            Write-Host "  3) sync repo to local vault        4) vault inventory"
            Write-Host "  5) download models                 6) commit + package + push"
            Write-Host "  7) open repo folder                q) quit"
            $c = Read-Host "choose"
            switch ($c) {
                "0" { & powershell -ExecutionPolicy Bypass -File $PSCommandPath desk }
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
        Write-Host "  claude | opencode                         engine sessions on local models"
        Write-Host "  desk                                      native desktop app (chat/missions/models/vault)"
        Write-Host "  menu                                      interactive menu (the desktop shortcut opens this)"
        Write-Host "  vault   init|sync|new|clone|list|backup   local private git remote"
        Write-Host "  models  [-Dest path] [-Only name]         download models (profile-aware)"
        Write-Host "  finish  [-SkipPush]                       commit + package + push + vault sync"
    }
}
