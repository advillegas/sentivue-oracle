# SentiVue Oracle - Windows uninstaller (double-click Uninstall.cmd at repo root).
$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
Write-Host "==================== SentiVue Oracle - Uninstall ===================="
Write-Host "  1) FULL clean      remove platform + models + IDE + shortcut"
Write-Host "  2) platform only   keep models and the IDE"
Write-Host "  q) cancel"
$c = Read-Host "choose"
if ($c -ne "1" -and $c -ne "2") { Write-Host "cancelled"; exit 0 }

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") stop
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "SentiVue Oracle.lnk") -Force
Remove-Item (Join-Path $Root ".tools"), (Join-Path $Root "state"), (Join-Path $Root "logs"), (Join-Path $Root ".worktrees") -Recurse -Force
Remove-Item (Join-Path $Root "serving\models.profile"), (Join-Path $Root "serving\tiers.env"), (Join-Path $Root "serving\llama-swap.rendered.win.yaml") -Force
Write-Host "==> platform components removed (serving, engines, shortcut, rendered config)"

if ($c -eq "1") {
    Remove-Item (Join-Path $Root "models"), (Join-Path $Root "incoming") -Recurse -Force
    Write-Host "==> models + quarantine removed"
    $ide = Read-Host "Also uninstall the IDE (VSCodium + its configs)? [y/N]"
    if ($ide -match "^[Yy]") {
        winget uninstall --id VSCodium.VSCodium -e --silent
        Remove-Item "$env:USERPROFILE\.continue" -Recurse -Force
        Write-Host "==> IDE removed"
    }
}
Write-Host ""
Write-Host "NOTE: the git vault (offline backup) is NEVER touched by this uninstaller."
Write-Host "To delete the repo folder itself, close everything using it and remove:"
Write-Host "  $Root"
Read-Host "Press Enter to close"
