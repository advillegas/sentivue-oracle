# download-models.ps1 - Resumable, policy-bound Windows model downloader.
#
#   powershell -ExecutionPolicy Bypass -File bootstrap\download-models.ps1
#   ... -Dest E:\oracle-models          # download straight onto an external drive
#   ... -Only qwen3-coder-30b           # just one model (smoke test)
#
# Same manifest (serving\models.manifest) and profile (serving\models.profile)
# as the Mac scripts. Resumable: re-run any time; finished files are skipped,
# partial files continue where they stopped.
#
# Getting the files to the Mac afterwards:
#   - copy the folder to ~/sentivue-oracle/models/  (folder-per-model layout), or
#   - keep them on the external drive and:  ln -s /Volumes/<drive>/oracle-models ~/sentivue-oracle/models
#   - use an exFAT-formatted drive (macOS treats NTFS as read-only, which still works for copying)
param(
    [string]$Dest = "",           # default: <repo>\models
    [string[]]$Only = @(),        # limit to specific model names
    [int]$Retries = 5
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Dest) { $Dest = Join-Path $Root "models" }
$DependencyCache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Write-Host "==> destination: $Dest"

$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
if (-not $curl) { Write-Host "ERROR: curl.exe not found (ships with Windows 10 1803+)"; exit 1 }
$python = Join-Path $Root "env\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not (Test-Path $python)) {
    Write-Host "ERROR: Python is required for model provenance."
    exit 1
}

$authHeader = @()
if ($env:HF_TOKEN) { $authHeader = @("-H", "Authorization: Bearer $($env:HF_TOKEN)") }

# ---- manifest + profile ------------------------------------------------------
$manifest = Join-Path $Root "serving\models.manifest"
$profileFile = Join-Path $Root "serving\models.profile"
$active = $null
if (Test-Path $profileFile) {
    $active = Get-Content $profileFile | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object { $_.Trim() }
}

$rows = Get-Content $manifest | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
    $f = $_ -split "\|"
    if ($f.Count -ne 7) { throw "invalid model manifest row: $_" }
    [pscustomobject]@{
        Name = $f[0].Trim()
        Repo = $f[1].Trim()
        Include = $f[2].Trim()
        RequestedRevision = $f[6].Trim()
        Revision = $f[6].Trim()
    }
} | Where-Object {
    ($null -eq $active -or $active -contains $_.Name) -and
    ($Only.Count -eq 0 -or $Only -contains $_.Name)
}
if (-not $rows) { Write-Host "Nothing to download (check -Only / serving\models.profile)"; exit 1 }

# ---- plan ---------------------------------------------------------------------
$authorityPath = Join-Path $Root "serving\model-authorities.json"
$authorityPayload = Get-Content -LiteralPath $authorityPath -Raw | ConvertFrom-Json
if ($authorityPayload.schema_version -ne 1 -or -not $authorityPayload.models) {
    throw "model authority policy is missing or unsupported"
}
$plan = @()
foreach ($r in $rows) {
    if ($r.Revision -notmatch "^[0-9a-f]{40}$") {
        throw "model $($r.Name) lacks a pinned revision"
    }
    $property = $authorityPayload.models.PSObject.Properties[$r.Name]
    if (-not $property) { throw "model authority is missing: $($r.Name)" }
    $authority = $property.Value
    if (
        $authority.repository -ne $r.Repo -or
        $authority.revision -ne $r.Revision -or
        $authority.include -ne $r.Include
    ) {
        throw "model authority differs from manifest: $($r.Name)"
    }
    $matched = @($authority.files)
    if (-not $matched) { throw "model authority has no files: $($r.Name)" }
    Write-Host ("==> locked {0}  ({1}@{2} :: {3})" -f $r.Name, $r.Repo, $r.Revision, $r.Include)
    foreach ($m in $matched) {
        $relative = [string]$m.path
        if (
            $relative.Contains("\") -or $relative.StartsWith("/") -or
            $relative -match "(^|/)\.\.?(/|$)" -or
            [string]$m.sha256 -notmatch "^[0-9a-f]{64}$" -or
            [long]$m.size -le 0
        ) {
            throw "invalid model authority file: $($r.Name)/$relative"
        }
        $encodedPath = (($relative -split "/") | ForEach-Object {
            [uri]::EscapeDataString($_)
        }) -join "/"
        $plan += [pscustomobject]@{
            Model = $r.Name; Repo = $r.Repo; Path = $relative
            Size = [long]$m.size; Sha256 = [string]$m.sha256
            Url = "https://huggingface.co/$($r.Repo)/resolve/$($r.Revision)/$encodedPath"
            Local = Join-Path (Join-Path $Dest $r.Name) ($relative -replace "/", "\")
        }
    }
}
if (-not $plan) { throw "model authority plan is empty" }

$totalGB = [math]::Round(($plan | Measure-Object Size -Sum).Sum / 1GB, 1)
$doneGB  = [math]::Round(($plan | Where-Object { (Test-Path $_.Local) } |
            ForEach-Object { (Get-Item $_.Local).Length } | Measure-Object -Sum).Sum / 1GB, 1)
$freeGB  = [math]::Round((Get-PSDrive (Split-Path -Qualifier $Dest).TrimEnd(":")).Free / 1GB)
Write-Host ""
Write-Host ("==> plan: {0} file(s), {1} GB total ({2} GB already on disk); {3} GB free on {4}" -f `
            @($plan).Count, $totalGB, $doneGB, $freeGB, (Split-Path -Qualifier $Dest))
if ($freeGB -lt ($totalGB - $doneGB + 30)) {
    throw "insufficient free space: need downloaded bytes plus 30 GB headroom; free space and re-run"
}

# ---- download (curl.exe: resume -C -, retries) ---------------------------------
$failed = @()
$n = 0
foreach ($p in $plan) {
    $n++
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p.Local) | Out-Null
    if ((Test-Path $p.Local) -and (Get-Item $p.Local).Length -eq $p.Size) {
        Write-Host ("[{0}/{1}] VERIFY: {2}" -f $n, @($plan).Count, $p.Path)
        $existingSha256 = (Get-FileHash -LiteralPath $p.Local -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existingSha256 -eq $p.Sha256) {
            Write-Host "    checksum matches promoted policy"
            continue
        }
        Write-Host "    existing complete-size file has the wrong checksum; replacing it"
        Remove-Item -LiteralPath $p.Local -Force
    }
    Write-Host ("[{0}/{1}] {2}\{3}  ({4} GB)" -f $n, @($plan).Count, $p.Model, $p.Path, [math]::Round($p.Size/1GB,1))
    $ok = $false
    for ($i = 1; $i -le $Retries -and -not $ok; $i++) {
        & $curl -L -C - --fail --retry 10 --retry-all-errors --connect-timeout 30 `
            --progress-bar @authHeader -o $p.Local $p.Url
        if ($LASTEXITCODE -eq 0 -and (Get-Item $p.Local).Length -eq $p.Size) {
            $downloadSha256 = (Get-FileHash -LiteralPath $p.Local -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($downloadSha256 -eq $p.Sha256) {
                $ok = $true
            } else {
                Write-Host "    checksum mismatch; removing corrupt completed file"
                Remove-Item -LiteralPath $p.Local -Force
            }
        }
        if (-not $ok) {
            Write-Host ("    attempt {0}/{1} failed (exit {2}) - retrying in 20 s, progress is kept" -f $i, $Retries, $LASTEXITCODE)
            if ($i -lt $Retries) { Start-Sleep -Seconds 20 }
        }
    }
    if (-not $ok) { $failed += "$($p.Model)/$($p.Path)" }
}

# ---- summary --------------------------------------------------------------------
Write-Host ""
Write-Host "==> sizes on disk:"
Get-ChildItem $Dest -Directory | ForEach-Object {
    $gb = [math]::Round((Get-ChildItem $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1GB, 1)
    Write-Host ("    {0,-22} {1,8} GB" -f $_.Name, $gb)
}
if ($failed) {
    Write-Host ("FAILED after {0} attempts: {1}  - re-run to resume." -f $Retries, ($failed -join ", "))
    exit 1
}
$authorityCopy = Join-Path ([IO.Path]::GetTempPath()) `
    ("oracle-model-authorities-" + [Guid]::NewGuid().ToString("N") + ".json")
try {
    Copy-Item -LiteralPath $authorityPath -Destination $authorityCopy
    foreach ($r in $rows) {
        & $python (Join-Path $Root "verification\lifecycle.py") import-model `
            --root $Root --cache $DependencyCache --models-root $Dest `
            --model-name $r.Name --authority $authorityCopy | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "model policy import failed: $($r.Name)"
        }
        Write-Host ("==> policy-bound snapshot recorded: {0}" -f $r.Name)
    }
} finally {
    Remove-Item -LiteralPath $authorityCopy -Force -ErrorAction SilentlyContinue
}
Write-Host ""
Write-Host "Policy-bound model acquisition complete."
