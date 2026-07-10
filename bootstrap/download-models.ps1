# download-models.ps1 — Windows model downloader for SentiVue Oracle.
#
# Downloads the model ensemble from Hugging Face on a Windows machine (e.g. to
# pre-download overnight or onto an external drive), for transfer to the Mac.
#
#   powershell -ExecutionPolicy Bypass -File bootstrap\download-models.ps1
#   ... -Dest E:\oracle-models          # download straight onto an external drive
#   ... -Only qwen3-coder-30b           # just one model (smoke test)
#
# Same manifest (serving\models.manifest) and profile (serving\models.profile)
# as the Mac scripts. Resumable: re-run any time, finished files are skipped.
#
# Getting the files to the Mac afterwards:
#   - copy the folder to ~/sentivue-oracle/models/   (folder-per-model layout), or
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

# ---- python + huggingface_hub (stable API, resumable, xet-accelerated) ------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
$pyArgs = @()
if (-not $python) {
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
    $pyArgs = @("-3")
}
if (-not $python) {
    Write-Host "ERROR: Python not found. Install it first:  winget install Python.Python.3.12"
    exit 1
}
& $python @pyArgs -c "import huggingface_hub" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> installing huggingface_hub (one time)"
    & $python @pyArgs -m pip install --user -U --quiet "huggingface_hub[cli]"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pip install huggingface_hub failed"; exit 1 }
}

# ---- manifest + profile ------------------------------------------------------
$manifest = Join-Path $Root "serving\models.manifest"
$profileFile = Join-Path $Root "serving\models.profile"
$active = $null
if (Test-Path $profileFile) {
    $active = Get-Content $profileFile | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object { $_.Trim() }
}

# rough sizes (GB) for the disk-space warning
$sizeGB = @{ "qwen3-coder-480b"=276; "kimi-k2-thinking"=381; "deepseek-v3.2"=320; "qwen3-coder-30b"=33; "qwen3-embedding-4b"=5 }

$rows = Get-Content $manifest | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
    $f = $_ -split "\|"
    [pscustomobject]@{
        Name    = $f[0].Trim()
        Repo    = $f[1].Trim()
        Include = $f[2].Trim()
    }
} | Where-Object {
    ($null -eq $active -or $active -contains $_.Name) -and
    ($Only.Count -eq 0 -or $Only -contains $_.Name)
}
if (-not $rows) { Write-Host "Nothing to download (check -Only / serving\models.profile)"; exit 1 }

$needGB = ($rows | ForEach-Object { $sizeGB[$_.Name] } | Measure-Object -Sum).Sum
$freeGB = [math]::Round((Get-PSDrive (Split-Path -Qualifier $Dest).TrimEnd(":")).Free / 1GB)
Write-Host ("==> plan: {0} model(s), ~{1} GB; {2} GB free on {3}" -f $rows.Count, $needGB, $freeGB, (Split-Path -Qualifier $Dest))
if ($freeGB -lt ($needGB + 30)) {
    Write-Host "WARN: free space looks tight — downloads resume, so you can free space and re-run."
}

# ---- download (resumable, retried) -------------------------------------------
$failed = @()
foreach ($r in $rows) {
    $target = Join-Path $Dest $r.Name
    Write-Host ""
    Write-Host ("==> {0}   ({1} :: {2})" -f $r.Name, $r.Repo, $r.Include)
    $ok = $false
    for ($i = 1; $i -le $Retries -and -not $ok; $i++) {
        & $python @pyArgs -c @"
from huggingface_hub import snapshot_download
snapshot_download(repo_id='$($r.Repo)', allow_patterns=['$($r.Include)'], local_dir=r'$target')
print('DOWNLOAD-COMPLETE: $($r.Name)')
"@
        if ($LASTEXITCODE -eq 0) { $ok = $true }
        else {
            Write-Host ("    attempt {0}/{1} failed — retrying in 30 s (progress is kept)" -f $i, $Retries)
            Start-Sleep -Seconds 30
        }
    }
    if (-not $ok) { $failed += $r.Name }
}

# ---- summary ------------------------------------------------------------------
Write-Host ""
Write-Host "==> sizes on disk:"
Get-ChildItem $Dest -Directory | ForEach-Object {
    $gb = [math]::Round((Get-ChildItem $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1GB, 1)
    Write-Host ("    {0,-22} {1,8} GB" -f $_.Name, $gb)
}
if ($failed) {
    Write-Host ("FAILED after {0} attempts: {1}  — re-run to resume." -f $Retries, ($failed -join ", "))
    exit 1
}
Write-Host ""
Write-Host "All downloads complete. Move '$Dest' to the Mac as ~/sentivue-oracle/models"
Write-Host "(or symlink it), then on the Mac:  oracle serve && oracle verify"
