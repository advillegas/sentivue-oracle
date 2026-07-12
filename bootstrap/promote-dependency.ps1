# Promote independently verified dependency identity/digest metadata.
param(
    [Parameter(Mandatory = $true)][string]$ArtifactId,
    [Parameter(Mandatory = $true)][string]$AuthorityFile
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = if ($env:ORACLE_PYTHON) {
    $env:ORACLE_PYTHON
} else {
    Join-Path $Root "env\.venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

& $Python (Join-Path $Root "verification\lifecycle.py") promote-authority `
    --root $Root --artifact-id $ArtifactId --authority $AuthorityFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
