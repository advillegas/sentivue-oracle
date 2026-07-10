# Deep white-label: rename the IDE's display identity to SentiVue Oracle.
# SURGICAL text replacement only - a JSON round-trip re-serializes the whole file
# and breaks extension activation (learned the hard way). Backup kept; idempotent.
$ErrorActionPreference = "Stop"
$pj = "$env:LOCALAPPDATA\Programs\VSCodium\resources\app\product.json"
if (-not (Test-Path $pj)) { Write-Host "IDE not installed - run setup-ide.ps1 install first"; exit 1 }
if (-not (Test-Path "$pj.bak")) { Copy-Item $pj "$pj.bak" }
$raw = Get-Content "$pj.bak" -Raw   # always patch from the pristine backup
$raw = $raw -replace '"nameShort":\s*"[^"]*"', '"nameShort": "SentiVue Oracle"'
$raw = $raw -replace '"nameLong":\s*"[^"]*"', '"nameLong": "SentiVue Oracle"'
try { $raw | ConvertFrom-Json | Out-Null } catch { Write-Host "patch invalid - aborting, file untouched"; exit 1 }
[IO.File]::WriteAllText($pj, $raw)
Write-Host "branded surgically: nameShort/nameLong -> SentiVue Oracle (restart the IDE)"
