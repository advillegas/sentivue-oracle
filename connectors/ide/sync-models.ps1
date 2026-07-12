[CmdletBinding()]
param(
    [string]$Root,
    [string]$HomePath
)

# Shared generator writes atomic UTF-8/no-BOM files, records ownership, and
# never edits tracked engine templates or unrelated Continue/Kilo user files.
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) {
    if ($env:ORACLE_ROOT) { $Root = $env:ORACLE_ROOT }
    else { $Root = Split-Path -Parent (Split-Path -Parent $ScriptRoot) }
}
if (-not $HomePath) {
    if ($env:ORACLE_HOME) { $HomePath = $env:ORACLE_HOME }
    else { $HomePath = $env:USERPROFILE }
}

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
    Write-Error "sync-models: Python 3.12 or newer is required"
    exit 127
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $Root "verification\lifecycle.py")
$Arguments += @("sync-config", "--root", $Root, "--home", $HomePath)
& $Python.Source @Arguments
exit $LASTEXITCODE
