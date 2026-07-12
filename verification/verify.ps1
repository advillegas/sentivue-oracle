[CmdletBinding()]
param(
    [switch]$StaticOnly,
    [string]$RunId,
    [string]$Root
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) {
    $Root = Split-Path -Parent $ScriptRoot
}

$Python = $null
$Prefix = @()
$Candidates = @(
    @{ Name = "python"; Prefix = @() },
    @{ Name = "py"; Prefix = @("-3") }
)
foreach ($Candidate in $Candidates) {
    $Command = Get-Command $Candidate.Name -ErrorAction SilentlyContinue
    if (-not $Command) {
        continue
    }
    $CandidatePrefix = @($Candidate.Prefix)
    & $Command.Source @CandidatePrefix -c "import sys; raise SystemExit(sys.version_info < (3, 12))" *> $null
    if ($LASTEXITCODE -eq 0) {
        $Python = $Command
        $Prefix = $CandidatePrefix
        break
    }
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

& $Python.Source @Arguments
exit $LASTEXITCODE
