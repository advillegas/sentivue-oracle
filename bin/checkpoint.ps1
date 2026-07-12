# checkpoint.ps1 "message" - commit staged work and append a ledger entry.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Message
)

$ErrorActionPreference = "Stop"

& git add -A
if ($LASTEXITCODE -ne 0) {
    throw "checkpoint: git add failed"
}

$Oversized = @()
$Staged = @(& git -c core.quotepath=false diff --cached --name-only --diff-filter=AM)
if ($LASTEXITCODE -ne 0) {
    throw "checkpoint: could not inspect staged files"
}
foreach ($RelativePath in $Staged) {
    if (-not $RelativePath) {
        continue
    }
    $FullPath = Join-Path (Get-Location).Path $RelativePath
    if (Test-Path -LiteralPath $FullPath -PathType Leaf) {
        $Size = (Get-Item -LiteralPath $FullPath).Length
        if ($Size -ge (50 * 1024 * 1024)) {
            $Oversized += "$RelativePath ($([Math]::Floor($Size / 1MB)) MB)"
        }
    }
}
if ($Oversized.Count -gt 0) {
    Write-Error (
        "checkpoint: REFUSED - staged files 50 MB or larger:`n  " +
        ($Oversized -join "`n  ") +
        "`ncheckpoint: unstage them and add them to .gitignore"
    )
    exit 1
}

& git diff --cached --quiet
$HasChanges = $LASTEXITCODE -ne 0
if ($HasChanges) {
    & git commit -q -m $Message
    if ($LASTEXITCODE -ne 0) {
        throw "checkpoint: git commit failed"
    }
} else {
    Write-Host "checkpoint: nothing to commit"
}

$Root = $env:ORACLE_ROOT
if (-not $Root) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$Memory = Join-Path $Root "memory"
New-Item -ItemType Directory -Force -Path $Memory | Out-Null
$Sha = (& git rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "checkpoint: could not resolve HEAD"
}
$Branch = Split-Path -Leaf (Get-Location).Path
$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$Entry = "- **$Timestamp** [checkpoint:$Branch] $Message ($Sha)"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::AppendAllText(
    (Join-Path $Memory "LEDGER.md"),
    $Entry + [Environment]::NewLine,
    $Utf8NoBom
)
Write-Host "checkpoint: $Message ($Sha)"
