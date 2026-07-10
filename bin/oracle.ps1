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
    "menu" {
        while ($true) {
            Write-Host ""
            Write-Host "==================== SentiVue Oracle ===================="
            Write-Host "  Windows node - $Root"
            Write-Host "========================================================="
            Write-Host "  1) sync repo to local vault        2) vault inventory"
            Write-Host "  3) pre-download models             4) commit + package + push (finish)"
            Write-Host "  5) open repo folder                q) quit"
            $c = Read-Host "choose"
            switch ($c) {
                "1" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") sync }
                "2" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") list }
                "3" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\download-models.ps1") }
                "4" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\finish-windows.ps1") }
                "5" { Start-Process explorer.exe $Root }
                "q" { return }
                default { }
            }
        }
    }
    default {
        Write-Host "oracle (Windows node)"
        Write-Host "  menu                                      interactive menu (the desktop shortcut opens this)"
        Write-Host "  vault   init|sync|new|clone|list|backup   local private git remote"
        Write-Host "  models  [-Dest path] [-Only name]         pre-download the model ensemble"
        Write-Host "  finish  [-SkipPush]                       commit + package + push + vault sync"
        Write-Host ""
        Write-Host "The full 'oracle' CLI (engines, missions, serving) runs on the Mac appliance."
    }
}
