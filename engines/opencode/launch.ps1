# Windows twin of launch.sh - OpenCode engine, repo-contained, local models.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:XDG_CONFIG_HOME = Join-Path $Root "engines\opencode\xdg"
$env:XDG_DATA_HOME = Join-Path $Root "engines\opencode\xdg-data"
$env:ORACLE_ROOT = $Root
$env:UV_OFFLINE = "1"
$env:UV_CACHE_DIR = Join-Path $Root "incoming\dependency-cache\uv"
$env:PATH = (Join-Path $Root ".tools\bin") + ";" + (Join-Path $Root ".tools\npm") + ";" + (Join-Path $Root ".tools\npm\node_modules\.bin") + ";" + $env:PATH
$env:OPENCODE_CONFIG = Join-Path $Root "state\generated\opencode\opencode.json"
$oc = Get-Command opencode -ErrorAction SilentlyContinue
if (-not $oc) { Write-Host "ERROR: opencode not installed - run: bin\oracle.ps1 setup"; exit 1 }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "connectors\ide\sync-models.ps1") *> $null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not (Test-Path -LiteralPath $env:OPENCODE_CONFIG)) {
    Write-Error "generated OpenCode config is unavailable; install a validated model snapshot"
    exit 1
}
try { Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null }
catch { Write-Host "WARN: llama-swap not responding - run: bin\oracle.ps1 serve" }
& opencode @args
exit $LASTEXITCODE
