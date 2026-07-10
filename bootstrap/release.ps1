# release.ps1 - publish downloadable installers as GitHub Release assets.
# Assets are built with 'git archive' (tracked files only - always clean):
#   sentivue-oracle-<ver>.tar.gz   Mac appliance (unpack -> bash install)
#   sentivue-oracle-<ver>.zip      Windows node  (unpack -> bin\oracle.ps1)
#
#   powershell -ExecutionPolicy Bypass -File bootstrap\release.ps1 -Version v0.1.0
param([string]$Version = "v0.1.0")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# ---- token from the stored git credential ------------------------------------
$credLines = ("protocol=https`nhost=github.com`n" | git credential fill) -split "`r?`n"
$token = ($credLines | Where-Object { $_ -like "password=*" }) -replace "^password=", ""
$headers = @{ Authorization = "token $token"; Accept = "application/vnd.github+json"; "User-Agent" = "oracle-release" }
$repo = "advillegas/sentivue-oracle"

# ---- tag ----------------------------------------------------------------------
git tag -f $Version | Out-Null
git push --quiet origin $Version --force
Write-Host "==> tagged $Version at $(git rev-parse --short HEAD)"

# ---- build assets (git archive = tracked files only) ---------------------------
$staging = Join-Path $env:TEMP "oracle-release"
New-Item -ItemType Directory -Force -Path $staging | Out-Null
$tarball = Join-Path $staging "sentivue-oracle-$Version.tar.gz"
$zipball = Join-Path $staging "sentivue-oracle-$Version.zip"
git archive --format=tar.gz --prefix=sentivue-oracle/ -o $tarball $Version
git archive --format=zip    --prefix=sentivue-oracle/ -o $zipball $Version
Write-Host ("==> assets: {0:N1} MB tar.gz, {1:N1} MB zip" -f ((Get-Item $tarball).Length/1MB), ((Get-Item $zipball).Length/1MB))

# ---- build double-clickable installers ------------------------------------------
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\build-installers.ps1") `
    -Version $Version -OutDir $staging
$macInstaller = Join-Path $staging "SentiVue-Oracle-Installer-$Version.command"
$winInstaller = Join-Path $staging "SentiVue-Oracle-Setup-$Version.cmd"

# ---- create (or reuse) the release ---------------------------------------------
$body = @"
Self-contained development ecosystem - offline agentic workstation.

## Installers (double-click, guided - no commands needed)

- **Mac appliance:** ``SentiVue-Oracle-Installer-$Version.command`` - double-click;
  a Terminal wizard prompts you through location, tools, model profile, downloads,
  and verification, and leaves a **SentiVue Oracle desktop shortcut** (menu: IDE,
  console, engines, envoy, status). First launch of a downloaded file:
  right-click -> Open (macOS Gatekeeper), once.
- **Windows node:** ``SentiVue-Oracle-Setup-$Version.cmd`` - double-click; a console
  wizard walks through location, git vault, optional model pre-download, and leaves
  a **SentiVue Oracle desktop shortcut** (menu: vault, models, finish). If
  SmartScreen interposes: More info -> Run anyway (unsigned installer).

## Plain archives (for scripted installs)

- ``sentivue-oracle-$Version.tar.gz`` (Mac): ``tar -xzf ... && cd sentivue-oracle && bash install``
- ``sentivue-oracle-$Version.zip`` (Windows): unpack, then ``bin\oracle.ps1``

Docs in README.md. Verify pins in VERSIONS.lock before first bootstrap.
"@
$rel = $null
try { $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$Version" -Headers $headers } catch {}
if (-not $rel) {
    $payload = @{ tag_name = $Version; name = "SentiVue Oracle $Version"; body = $body; draft = $false } | ConvertTo-Json
    $rel = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$repo/releases" -Headers $headers -Body $payload -ContentType "application/json"
    Write-Host "==> release created: $($rel.html_url)"
} else {
    Write-Host "==> release exists: $($rel.html_url) (replacing assets)"
    foreach ($a in $rel.assets) {
        Invoke-RestMethod -Method Delete -Uri "https://api.github.com/repos/$repo/releases/assets/$($a.id)" -Headers $headers | Out-Null
    }
}

# ---- upload assets --------------------------------------------------------------
$assets = @($macInstaller, $winInstaller, $tarball, $zipball)
foreach ($f in $assets) {
    $name = Split-Path -Leaf $f
    $ctype = "application/octet-stream"
    if ($name.EndsWith(".gz"))  { $ctype = "application/gzip" }
    if ($name.EndsWith(".zip")) { $ctype = "application/zip" }
    $up = "https://uploads.github.com/repos/$repo/releases/$($rel.id)/assets?name=$name"
    Invoke-RestMethod -Method Post -Uri $up -Headers $headers -ContentType $ctype -InFile $f | Out-Null
    Write-Host "==> uploaded $name"
}
Write-Host ""
Write-Host "DONE: $($rel.html_url)"
Write-Host "Download (with gh): gh release download $Version -R $repo"
