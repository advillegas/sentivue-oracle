# Windows twin of launch.sh - Claude Code engine, repo-contained, local models.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:CLAUDE_CONFIG_DIR = Join-Path $Root "engines\claude-code\home"
$env:ORACLE_ROOT = $Root
$env:PATH = (Join-Path $Root ".tools\npm") + ";" + (Join-Path $Root ".tools\npm\node_modules\.bin") + ";" + $env:PATH
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) { Write-Host "ERROR: claude not installed - run: bin\oracle.ps1 setup"; exit 1 }
try { Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null }
catch { Write-Host "WARN: llama-swap not responding - run: bin\oracle.ps1 serve" }
& claude --mcp-config (Join-Path $Root "connectors\mcp.claude.json") @args
exit $LASTEXITCODE
