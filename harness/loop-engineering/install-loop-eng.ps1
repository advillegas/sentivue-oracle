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
$DependencyCache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
$ArtifactManifest = Join-Path $DependencyCache "manifest.json"
$Python = Join-Path $Root "env\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

$pins = @{}
Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" } | ForEach-Object {
    $kv = $_ -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim()
}

& $Python (Join-Path $Root "verification\lifecycle.py") preflight-source `
    --root $Root --manifest $ArtifactManifest --cache $DependencyCache `
    --artifact-id "source-loop-engineering" --destination $Vendor --trusted-root $Root `
    --expected-version $pins['LOOP_ENG_COMMIT'] `
    --expected-requested-version $pins['LOOP_ENG_PIN'] | Out-Null
if ($LASTEXITCODE -ne 0) { throw "loop-engineering source install preflight failed" }
& $Python (Join-Path $Root "verification\lifecycle.py") install-source `
    --root $Root --manifest $ArtifactManifest --cache $DependencyCache `
    --artifact-id "source-loop-engineering" --destination $Vendor --trusted-root $Root `
    --expected-version $pins['LOOP_ENG_COMMIT'] `
    --expected-requested-version $pins['LOOP_ENG_PIN'] | Out-Null
if ($LASTEXITCODE -ne 0) { throw "loop-engineering policy-bound source install failed" }
& $Python (Join-Path $Root "verification\lifecycle.py") validate-source `
    --root $Root --manifest $ArtifactManifest --cache $DependencyCache `
    --artifact-id "source-loop-engineering" --destination $Vendor --trusted-root $Root `
    --expected-version $pins['LOOP_ENG_COMMIT'] `
    --expected-requested-version $pins['LOOP_ENG_PIN'] | Out-Null
if ($LASTEXITCODE -ne 0) { throw "loop-engineering source identity validation failed" }

Write-Host "==> loop CLIs (pinned, repo-local npm prefix)"
$env:npm_config_prefix = Join-Path $Root ".tools\npm"
$env:npm_config_cache = Join-Path $DependencyCache "npm"
$env:npm_config_offline = "true"
New-Item -ItemType Directory -Force -Path $env:npm_config_prefix | Out-Null
function Get-CachedArtifact([string]$Id, [string]$Version) {
    $path = & $Python (Join-Path $Root "verification\lifecycle.py") artifact-path `
        --manifest $ArtifactManifest --cache $DependencyCache --artifact-id $Id `
        --expected-version $Version --expected-requested-version $Version `
        --root $Root --reproducible
    if ($LASTEXITCODE -ne 0) { throw "cached artifact validation failed: $Id" }
    return $path.Trim()
}
$loopAudit = Get-CachedArtifact "npm-loop-audit" $pins['LOOP_AUDIT_NPM']
$loopInit = Get-CachedArtifact "npm-loop-init" $pins['LOOP_INIT_NPM']
$loopCost = Get-CachedArtifact "npm-loop-cost" $pins['LOOP_COST_NPM']
$loopSync = Get-CachedArtifact "npm-loop-sync" $pins['LOOP_SYNC_NPM']
npm install -g --offline --no-audit --no-fund `
    $loopAudit $loopInit $loopCost $loopSync | Out-Null
if ($LASTEXITCODE -ne 0) { throw "offline loop CLI install failed" }

# distilled skill so engines can pull the patterns without loading the repo
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\sync-skills.ps1")
Write-Host "==> loop-engineering installed: patterns in harness\loop-engineering\vendor, CLIs in .tools\npm"
Write-Host "    try: bin\oracle.ps1 loops audit"
