[CmdletBinding()]
param(
    [switch]$StaticOnly,
    [string]$RunId,
    [string]$Root,
    [string]$ReportRoot
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) {
    $Root = Split-Path -Parent $ScriptRoot
}

$Python = Get-Command "python" -ErrorAction SilentlyContinue
$Prefix = @()
if (-not $Python) {
    $Python = Get-Command "py" -ErrorAction SilentlyContinue
    $Prefix = @("-3")
}
if (-not $Python) {
    Write-Error "verification: Python 3.12 or newer is required"
    exit 127
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $ScriptRoot "verify.py")
$Arguments += @("--root", $Root)
if ($StaticOnly) {
    $Arguments += "--static-only"
}
if ($RunId) {
    $Arguments += @("--run-id", $RunId)
}
if ($ReportRoot) {
    $Arguments += @("--report-root", $ReportRoot)
}

& $Python.Source @Arguments
exit $LASTEXITCODE
