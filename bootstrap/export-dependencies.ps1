[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArtifactId,
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$RequestedVersion,
    [Parameter(Mandatory = $true)][string]$ResolvedVersion,
    [string]$ExpectedSha256,
    [string]$CacheDir,
    [string]$Root,
    [switch]$Trusted
)

# This is the only lifecycle entry point that fetches dependency bytes. Every
# result is copied into an explicit cache and recorded with exact resolution,
# source URL, byte count, and SHA-256 before it can be used reproducibly.
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = Split-Path -Parent $ScriptRoot }
if (-not $CacheDir) { $CacheDir = Join-Path $Root "incoming\dependency-cache" }

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
    Write-Error "dependency export: Python 3.12 or newer is required"
    exit 127
}

$Arguments = @($Prefix)
$Arguments += (Join-Path $Root "verification\lifecycle.py")
$Arguments += @(
    "export-artifact",
    "--cache", $CacheDir,
    "--artifact-id", $ArtifactId,
    "--url", $Url,
    "--requested-version", $RequestedVersion,
    "--resolved-version", $ResolvedVersion
)
if ($ExpectedSha256) {
    $Arguments += @("--expected-sha256", $ExpectedSha256)
}
if ($Trusted) {
    $Arguments += @("--trusted", "--root", $Root)
} else {
    Write-Warning "recording untrusted acquisition evidence; release/install will reject it"
}

& $Python.Source @Arguments
exit $LASTEXITCODE
