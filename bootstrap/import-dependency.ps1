# Import an offline archive already bound by VERSIONS.lock and policy.json.
param(
    [Parameter(Mandatory = $true)][string]$ArtifactId,
    [Parameter(Mandatory = $true)][string]$SourceFile,
    [Parameter(Mandatory = $true)][string]$SourceUrl,
    [Parameter(Mandatory = $true)][string]$RequestedVersion,
    [Parameter(Mandatory = $true)][string]$ResolvedVersion
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
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

& $Python (Join-Path $Root "verification\lifecycle.py") import-artifact `
    --root $Root --cache $Cache --artifact-id $ArtifactId `
    --file $SourceFile --url $SourceUrl `
    --requested-version $RequestedVersion --resolved-version $ResolvedVersion
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
