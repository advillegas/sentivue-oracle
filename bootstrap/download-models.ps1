# download-models.ps1 - Windows model downloader for SentiVue Oracle.
# Zero dependencies: uses the Hugging Face API for file listings and Windows'
# built-in curl.exe for resumable downloads. No Python required.
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
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Write-Host "==> destination: $Dest"

$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
if (-not $curl) { Write-Host "ERROR: curl.exe not found (ships with Windows 10 1803+)"; exit 1 }

$authHeader = @()
if ($env:HF_TOKEN) { $authHeader = @("-H", "Authorization: Bearer $($env:HF_TOKEN)") }

function Get-RepoFiles([string]$repo) {
    # Full recursive file listing, following API pagination if present.
    $files = @()
    $uri = "https://huggingface.co/api/models/$repo/tree/main?recursive=true"
    while ($uri) {
        $headers = @{}
        if ($env:HF_TOKEN) { $headers["Authorization"] = "Bearer $($env:HF_TOKEN)" }
        $resp = Invoke-WebRequest -Uri $uri -Headers $headers -UseBasicParsing
        $files += ($resp.Content | ConvertFrom-Json) | Where-Object { $_.type -eq "file" }
        $uri = $null
        if ($resp.Headers.Link -and $resp.Headers.Link -match '<([^>]+)>;\s*rel="next"') { $uri = $Matches[1] }
    }
    return $files
}

# ---- manifest + profile ------------------------------------------------------
$manifest = Join-Path $Root "serving\models.manifest"
$profileFile = Join-Path $Root "serving\models.profile"
$active = $null
if (Test-Path $profileFile) {
    $active = Get-Content $profileFile | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object { $_.Trim() }
}

$rows = Get-Content $manifest | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
    $f = $_ -split "\|"
    [pscustomobject]@{ Name = $f[0].Trim(); Repo = $f[1].Trim(); Include = $f[2].Trim() }
} | Where-Object {
    ($null -eq $active -or $active -contains $_.Name) -and
    ($Only.Count -eq 0 -or $Only -contains $_.Name)
}
if (-not $rows) { Write-Host "Nothing to download (check -Only / serving\models.profile)"; exit 1 }

# ---- plan ---------------------------------------------------------------------
$plan = @()
foreach ($r in $rows) {
    Write-Host ("==> listing {0}  ({1} :: {2})" -f $r.Name, $r.Repo, $r.Include)
    $matched = Get-RepoFiles $r.Repo | Where-Object { $_.path -like $r.Include }
    if (-not $matched) { Write-Host "    WARN: no files match pattern '$($r.Include)'"; continue }
    foreach ($m in $matched) {
        $plan += [pscustomobject]@{
            Model = $r.Name; Repo = $r.Repo; Path = $m.path; Size = [long]$m.size
            Url   = "https://huggingface.co/$($r.Repo)/resolve/main/$($m.path)"
            Local = Join-Path (Join-Path $Dest $r.Name) ($m.path -replace "/", "\")
        }
    }
}
if (-not $plan) { Write-Host "ERROR: nothing matched - check include patterns in the manifest."; exit 1 }

$totalGB = [math]::Round(($plan | Measure-Object Size -Sum).Sum / 1GB, 1)
$doneGB  = [math]::Round(($plan | Where-Object { (Test-Path $_.Local) } |
            ForEach-Object { (Get-Item $_.Local).Length } | Measure-Object -Sum).Sum / 1GB, 1)
$freeGB  = [math]::Round((Get-PSDrive (Split-Path -Qualifier $Dest).TrimEnd(":")).Free / 1GB)
Write-Host ""
Write-Host ("==> plan: {0} file(s), {1} GB total ({2} GB already on disk); {3} GB free on {4}" -f `
            @($plan).Count, $totalGB, $doneGB, $freeGB, (Split-Path -Qualifier $Dest))
if ($freeGB -lt ($totalGB - $doneGB + 30)) {
    Write-Host "WARN: free space looks tight - downloads resume, so you can free space and re-run."
}

# ---- download (curl.exe: resume -C -, retries) ---------------------------------
$failed = @()
$n = 0
foreach ($p in $plan) {
    $n++
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p.Local) | Out-Null
    if ((Test-Path $p.Local) -and (Get-Item $p.Local).Length -eq $p.Size) {
        Write-Host ("[{0}/{1}] SKIP (complete): {2}" -f $n, @($plan).Count, $p.Path)
        continue
    }
    Write-Host ("[{0}/{1}] {2}\{3}  ({4} GB)" -f $n, @($plan).Count, $p.Model, $p.Path, [math]::Round($p.Size/1GB,1))
    $ok = $false
    for ($i = 1; $i -le $Retries -and -not $ok; $i++) {
        & $curl -L -C - --fail --retry 10 --retry-all-errors --connect-timeout 30 `
            --progress-bar @authHeader -o $p.Local $p.Url
        if ($LASTEXITCODE -eq 0 -and (Get-Item $p.Local).Length -eq $p.Size) { $ok = $true }
        else {
            Write-Host ("    attempt {0}/{1} failed (exit {2}) - retrying in 20 s, progress is kept" -f $i, $Retries, $LASTEXITCODE)
            Start-Sleep -Seconds 20
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
Write-Host ""
Write-Host "All downloads complete. Move '$Dest' to the Mac as ~/sentivue-oracle/models"
Write-Host "(or symlink it), then on the Mac:  oracle serve; oracle verify"
