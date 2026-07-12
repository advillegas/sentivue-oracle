# Windows twin of launch.sh - Claude Code engine, repo-contained, local models.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:CLAUDE_CONFIG_DIR = Join-Path $Root "engines\claude-code\home"
$env:ORACLE_ROOT = $Root
$env:UV_OFFLINE = "1"
$env:UV_CACHE_DIR = Join-Path $Root "incoming\dependency-cache\uv"
$env:PATH = (Join-Path $Root ".tools\bin") + ";" + (Join-Path $Root ".tools\npm") + ";" + (Join-Path $Root ".tools\npm\node_modules\.bin") + ";" + $env:PATH
$GeneratedSettings = Join-Path $Root "state\generated\claude-code\settings.json"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) { Write-Host "ERROR: claude not installed - run: bin\oracle.ps1 setup"; exit 1 }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "connectors\ide\sync-models.ps1") *> $null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not (Test-Path -LiteralPath $GeneratedSettings)) {
    Write-Error "generated Claude settings are unavailable; install a validated model snapshot"
    exit 1
}
try { Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null }
catch { Write-Host "WARN: llama-swap not responding - run: bin\oracle.ps1 serve" }
& claude --settings $GeneratedSettings --mcp-config (Join-Path $Root "connectors\mcp.claude.json") @args
exit $LASTEXITCODE
