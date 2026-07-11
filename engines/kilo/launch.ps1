# Windows launcher for the Kilo engine - repo-contained, local models.
# Kilo reads ~\.config\kilo\kilo.jsonc, which sync-models.ps1 generates against
# llama-swap (openai-compatible provider, telemetry off, sharing off).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:ORACLE_ROOT = $Root
$env:PATH = (Join-Path $Root ".tools\npm") + ";" + (Join-Path $Root ".tools\npm\node_modules\.bin") + ";" + $env:PATH
$kilo = Get-Command kilo -ErrorAction SilentlyContinue
if (-not $kilo) { Write-Host "ERROR: kilo not installed - run: bin\oracle.ps1 setup"; exit 1 }
if (-not (Test-Path "$env:USERPROFILE\.config\kilo\kilo.jsonc")) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "connectors\ide\sync-models.ps1") | Out-Null
}
try { Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null }
catch { Write-Host "WARN: llama-swap not responding - run: bin\oracle.ps1 serve" }
& kilo @args
exit $LASTEXITCODE
