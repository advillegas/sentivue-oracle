[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Revision = "HEAD",
    [string]$OutDir,
    [string]$Root
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = Split-Path -Parent $ScriptRoot }
if (-not $OutDir) { $OutDir = Join-Path $Root "artifacts\releases\$Version" }

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
    Write-Error "package: Python 3.12 or newer is required"
    exit 127
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $Root "verification\lifecycle.py")
$Arguments += @(
    "package",
    "--root", $Root,
    "--version", $Version,
    "--revision", $Revision,
    "--output", $OutDir
)
& $Python.Source @Arguments
exit $LASTEXITCODE
