[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Revision = "HEAD",
    [string]$OutDir,
    [string]$DependencyCache,
    [string]$Root
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = Split-Path -Parent $ScriptRoot }
if (-not $OutDir) { $OutDir = Join-Path $Root "artifacts\installers\$Version" }

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
    Write-Error "installer build: Python 3.12 or newer is required"
    exit 127
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $Root "verification\lifecycle.py")
$Arguments += @(
    "installers",
    "--root", $Root,
    "--version", $Version,
    "--revision", $Revision,
    "--output", $OutDir
)
if ($DependencyCache) {
    $Arguments += @("--dependency-cache", $DependencyCache)
}
& $Python.Source @Arguments
exit $LASTEXITCODE
