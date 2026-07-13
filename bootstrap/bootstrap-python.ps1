[CmdletBinding()]
param(
    [string]$Root,
    [string]$CacheDir
)

# Establish the pinned Python trust root using only Windows-native primitives.
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = Split-Path -Parent $ScriptRoot }
if (-not $CacheDir) { $CacheDir = Join-Path $Root "incoming\dependency-cache" }
$Pins = @{}
Get-Content -LiteralPath (Join-Path $Root "VERSIONS.lock") |
    Where-Object { $_ -match "=" } |
    ForEach-Object {
        $Pair = $_ -split "=", 2
        $Pins[$Pair[0].Trim()] = ($Pair[1] -split "#", 2)[0].Trim()
    }

$Version = $Pins["PYTHON_VERSION"]
$ExpectedSha256 = $Pins["PYTHON_WINDOWS_X64_SHA256"]
$Url = "https://www.python.org/ftp/python/$Version/python-$Version-embed-amd64.zip"
if (
    $Version -notmatch "^[0-9]+\.[0-9]+\.[0-9]+$" -or
    $ExpectedSha256 -notmatch "^[0-9a-f]{64}$"
) {
    throw "portable Python policy is unresolved"
}

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
$Download = Join-Path $CacheDir "python-$Version-embed-amd64.zip.part"
$DownloadComplete = Join-Path $CacheDir "python-$Version-embed-amd64.zip"
if (Test-Path -LiteralPath $DownloadComplete -PathType Leaf) {
    if (
        (Get-FileHash -LiteralPath $DownloadComplete -Algorithm SHA256).Hash.ToLowerInvariant() `
            -ne $ExpectedSha256
    ) {
        Remove-Item -LiteralPath $DownloadComplete -Force
    }
}
if (-not (Test-Path -LiteralPath $DownloadComplete -PathType Leaf)) {
    Write-Host "==> downloading pinned portable Python $Version"
    $Curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($Curl) {
        & $Curl.Source -L -C - --fail --retry 10 --retry-all-errors `
            --connect-timeout 30 --progress-bar -o $Download $Url
        if ($LASTEXITCODE -ne 0) {
            throw "portable Python download failed: $LASTEXITCODE"
        }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $Download -UseBasicParsing
    }
    if (
        (Get-FileHash -LiteralPath $Download -Algorithm SHA256).Hash.ToLowerInvariant() `
            -ne $ExpectedSha256
    ) {
        Remove-Item -LiteralPath $Download -Force -ErrorAction SilentlyContinue
        throw "portable Python checksum mismatch"
    }
    Move-Item -LiteralPath $Download -Destination $DownloadComplete -Force
}

$Destination = Join-Path $Root ".tools\python-bootstrap"
$Python = Join-Path $Destination "python.exe"
$Valid = $false
if (Test-Path -LiteralPath $Python -PathType Leaf) {
    $Actual = (& $Python -c "import platform; print(platform.python_version())").Trim()
    $Valid = $LASTEXITCODE -eq 0 -and $Actual -eq $Version
}
if (-not $Valid) {
    $Stage = Join-Path $Root (".python-bootstrap-stage-" + [Guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Path $Stage | Out-Null
        Expand-Archive -LiteralPath $DownloadComplete -DestinationPath $Stage
        $StagedPython = Join-Path $Stage "python.exe"
        if (-not (Test-Path -LiteralPath $StagedPython -PathType Leaf)) {
            throw "portable Python archive has no python.exe"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) |
            Out-Null
        Remove-Item -LiteralPath $Destination -Recurse -Force `
            -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $Stage -Destination $Destination
        $Stage = $null
    } finally {
        if ($Stage) {
            Remove-Item -LiteralPath $Stage -Recurse -Force `
                -ErrorAction SilentlyContinue
        }
    }
}
$Actual = (& $Python -c "import platform; print(platform.python_version())").Trim()
if ($LASTEXITCODE -ne 0 -or $Actual -ne $Version) {
    throw "portable Python runtime does not match $Version"
}

& $Python (Join-Path $Root "verification\lifecycle.py") import-artifact `
    --root $Root --cache $CacheDir `
    --artifact-id "python-bootstrap-windows-x64" `
    --file $DownloadComplete --url $Url `
    --requested-version $Version --resolved-version $Version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "portable Python policy import failed" }
Write-Output $Python
