# Windows launcher for the Kilo engine - repo-contained, local models.
# Kilo reads state\generated\kilo\kilo.jsonc, generated against
# llama-swap (openai-compatible provider, telemetry off, sharing off).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:ORACLE_ROOT = $Root
$env:UV_OFFLINE = "1"
$env:UV_CACHE_DIR = Join-Path $Root "incoming\dependency-cache\uv"
$env:PATH = (Join-Path $Root ".tools\bin") + ";" + (Join-Path $Root ".tools\npm") + ";" + (Join-Path $Root ".tools\npm\node_modules\.bin") + ";" + $env:PATH
$env:KILO_CONFIG = Join-Path $Root "state\generated\kilo\kilo.jsonc"
$env:OPENCODE_CONFIG = $env:KILO_CONFIG
# hardening profile: disable every Kilo call-home path (telemetry, sharing,
# gateway, update, remote model discovery, ...). See engines\kilo\HARDENING.md.
. (Join-Path $PSScriptRoot "hardened-env.ps1")
$kilo = Get-Command kilo -ErrorAction SilentlyContinue
if (-not $kilo) { Write-Host "ERROR: kilo not installed - run: bin\oracle.ps1 setup"; exit 1 }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "connectors\ide\sync-models.ps1") | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not (Test-Path -LiteralPath $env:KILO_CONFIG)) {
    Write-Error "generated Kilo config is unavailable; install a validated model snapshot"
    exit 1
}
try { Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null }
catch { Write-Host "WARN: llama-swap not responding - run: bin\oracle.ps1 serve" }
& kilo @args
exit $LASTEXITCODE
