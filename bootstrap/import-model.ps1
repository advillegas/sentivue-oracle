# Import local shards only after expected identities were independently
# promoted into serving\model-authorities.json.
param(
    [Parameter(Mandatory = $true)][string]$ModelName,
    [Parameter(Mandatory = $true)][string]$AuthorityFile,
    [string]$ModelsRoot = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $ModelsRoot) { $ModelsRoot = Join-Path $Root "models" }
$Cache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
$Python = if ($env:ORACLE_PYTHON) {
    $env:ORACLE_PYTHON
} else {
    Join-Path $Root "env\.venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

& $Python (Join-Path $Root "verification\lifecycle.py") import-model `
    --root $Root --cache $Cache --models-root $ModelsRoot `
    --model-name $ModelName --authority $AuthorityFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
