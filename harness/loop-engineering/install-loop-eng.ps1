# install-loop-eng.ps1 - loop-engineering toolkit (patterns + CLIs).
# Vendors the reference repo (pinned) for its 7 production loop patterns,
# failure-mode/anti-pattern docs, and primitives matrix; installs the npm
# CLIs (loop-audit, loop-init, loop-cost, loop-sync) into the repo-local
# toolchain; and syncs a distilled skill so both engines can cite it.
#
#   powershell -File harness\loop-engineering\install-loop-eng.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Vendor = Join-Path $PSScriptRoot "vendor"

$pins = @{}
Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" } | ForEach-Object {
    $kv = $_ -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim()
}

if (-not (Test-Path (Join-Path $Vendor ".git"))) {
    Write-Host "==> cloning loop-engineering $($pins['LOOP_ENG_PIN']) (shallow, pinned)"
    git clone --depth 1 --branch $pins['LOOP_ENG_PIN'] $pins['LOOP_ENG_REPO'] $Vendor
} else {
    Write-Host "==> loop-engineering vendor checkout present"
}

Write-Host "==> loop CLIs (pinned, repo-local npm prefix)"
$env:npm_config_prefix = Join-Path $Root ".tools\npm"
New-Item -ItemType Directory -Force -Path $env:npm_config_prefix | Out-Null
npm install -g --no-audit --no-fund `
    "@cobusgreyling/loop-audit@$($pins['LOOP_AUDIT_NPM'])" `
    "@cobusgreyling/loop-init@$($pins['LOOP_INIT_NPM'])" `
    "@cobusgreyling/loop-cost@$($pins['LOOP_COST_NPM'])" `
    "@cobusgreyling/loop-sync@$($pins['LOOP_SYNC_NPM'])" | Out-Null

# distilled skill so engines can pull the patterns without loading the repo
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\sync-skills.ps1")
Write-Host "==> loop-engineering installed: patterns in harness\loop-engineering\vendor, CLIs in .tools\npm"
Write-Host "    try: bin\oracle.ps1 loops audit"
