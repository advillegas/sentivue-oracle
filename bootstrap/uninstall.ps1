[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Purge,
    [switch]$ConfirmPurge,
    [string]$Root,
    [string]$HomePath
)

# Dry-run is the default. Only files recorded in .install-state are considered.
# Purge is separate, confirmed, and remains confined to Oracle runtime roots.
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = Split-Path -Parent $ScriptRoot }
if (-not $HomePath) { $HomePath = $env:USERPROFILE }

$Python = $null
$Prefix = @()
foreach ($Candidate in @(
    @{ Name = "python"; Prefix = @() },
    @{ Name = "py"; Prefix = @("-3") }
)) {
    $Command = Get-Command $Candidate.Name -ErrorAction SilentlyContinue
    if (-not $Command) { continue }
    $CandidatePrefix = @($Candidate.Prefix)
    & $Command.Source @CandidatePrefix -c "import sys; raise SystemExit(sys.version_info < (3, 12))" *> $null
    if ($LASTEXITCODE -eq 0) {
        $Python = $Command
        $Prefix = $CandidatePrefix
        break
    }
}
if (-not $Python) {
    Write-Error "uninstall: Python 3.12 or newer is required"
    exit 127
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $Root "verification\lifecycle.py")
$Arguments += @("uninstall", "--root", $Root, "--home", $HomePath)
if ($Apply) { $Arguments += "--apply" }
if ($Purge) { $Arguments += "--purge" }
if ($ConfirmPurge) { $Arguments += "--confirm-purge" }

& $Python.Source @Arguments
exit $LASTEXITCODE
