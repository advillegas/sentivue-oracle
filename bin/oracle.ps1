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
    default {
        Write-Host "oracle (Windows node)"
        Write-Host "  vault   init|sync|new|clone|list|backup   local private git remote"
        Write-Host "  models  [-Dest path] [-Only name]         pre-download the model ensemble"
        Write-Host "  finish  [-SkipPush]                       commit + package + push + vault sync"
        Write-Host ""
        Write-Host "The full 'oracle' CLI (engines, missions, serving) runs on the Mac appliance."
    }
}
