# Deep white-label: patch the installed IDE's product identity to SentiVue Oracle.
# Safe fields only (display names); a .bak backup is kept; updates are disabled in
# our settings so the patch persists. Re-run any time (idempotent).
$ErrorActionPreference = "Stop"
$app = "$env:LOCALAPPDATA\Programs\VSCodium\resources\app"
$pj = Join-Path $app "product.json"
if (-not (Test-Path $pj)) { Write-Host "IDE not installed - run setup-ide.ps1 install first"; exit 1 }
if (-not (Test-Path "$pj.bak")) { Copy-Item $pj "$pj.bak" }
$p = Get-Content $pj -Raw | ConvertFrom-Json
$p.nameShort = "SentiVue Oracle"
$p.nameLong = "SentiVue Oracle"
if ($p.PSObject.Properties.Name -contains "applicationName") { } # binary name untouched
$p | ConvertTo-Json -Depth 20 | Set-Content -Path $pj -Encoding UTF8
try { Get-Content $pj -Raw | ConvertFrom-Json | Out-Null; Write-Host "branded: nameShort/nameLong -> SentiVue Oracle (restart the IDE)" }
catch { Copy-Item "$pj.bak" $pj -Force; Write-Host "patch produced invalid JSON - restored backup"; exit 1 }
