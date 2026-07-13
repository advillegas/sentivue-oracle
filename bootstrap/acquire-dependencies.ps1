[CmdletBinding()]
param(
    [string]$CacheDir,
    [string]$Root,
    [int]$Retries = 3,
    [switch]$IncludeOptional
)

# Connected installer boundary: download only committed, checksum-bound inputs.
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = Split-Path -Parent $ScriptRoot }
if (-not $CacheDir) {
    $CacheDir = if ($env:ORACLE_DEPENDENCY_CACHE) {
        $env:ORACLE_DEPENDENCY_CACHE
    } else {
        Join-Path $Root "incoming\dependency-cache"
    }
}

$Python = $null
$Prefix = @()
foreach ($Candidate in @(
    @{ Name = (Join-Path $Root "env\.venv\Scripts\python.exe"); Prefix = @() },
    @{ Name = (Join-Path $Root ".tools\python-bootstrap\python.exe"); Prefix = @() },
    @{ Name = "python"; Prefix = @() },
    @{ Name = "py"; Prefix = @("-3") }
)) {
    $Command = Get-Command $Candidate.Name -ErrorAction SilentlyContinue
    if (-not $Command) { continue }
    $CandidatePrefix = @($Candidate.Prefix)
    & $Command.Source @CandidatePrefix -c `
        "import sys; raise SystemExit(sys.version_info < (3, 12))" *> $null
    if ($LASTEXITCODE -eq 0) {
        $Python = $Command.Source
        $Prefix = $CandidatePrefix
        break
    }
}
if (-not $Python) {
    throw "connected dependency acquisition requires Python 3.12 or newer"
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $Root "verification\lifecycle.py")
$Arguments += @(
    "acquire-dependencies",
    "--root", $Root,
    "--cache", $CacheDir,
    "--platform", "windows-x64",
    "--retries", [string]$Retries
)
if ($IncludeOptional) { $Arguments += "--include-optional" }

& $Python @Arguments
exit $LASTEXITCODE
