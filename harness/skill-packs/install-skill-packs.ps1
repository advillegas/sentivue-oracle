# install-skill-packs.ps1 - vendor pinned third-party skill frameworks and sync
# them into both engines: obra/superpowers (methodology: brainstorm -> plan ->
# TDD -> two-stage review) and garrytan/gstack (23 role specialists: CEO/eng/
# design reviews, QA, and retro). Only skills admitted by offline-policy.json
# are linked. Network-capable instructions are flagged and excluded.
#
#   powershell -File harness\skill-packs\install-skill-packs.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$DependencyCache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
$ArtifactManifest = Join-Path $DependencyCache "manifest.json"
$OfflinePolicy = Join-Path $PSScriptRoot "offline-policy.json"
$Serving = Join-Path $Root "verification\serving.py"
$Python = Join-Path $Root "env\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

$pins = @{}
Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" } | ForEach-Object {
    $kv = $_ -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim()
}

function Get-Vendored(
    [string]$Name,
    [string]$ArtifactId,
    [string]$Requested,
    [string]$Resolved
) {
    $vendor = Join-Path $PSScriptRoot "vendor\$Name"
    $lifecycle = Join-Path $Root "verification\lifecycle.py"
    & $Python $lifecycle preflight-source --root $Root --manifest $ArtifactManifest `
        --cache $DependencyCache --artifact-id $ArtifactId --destination $vendor `
        --trusted-root $Root --expected-version $Resolved `
        --expected-requested-version $Requested | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$Name source install preflight failed" }
    & $Python $lifecycle install-source --root $Root --manifest $ArtifactManifest `
        --cache $DependencyCache --artifact-id $ArtifactId --destination $vendor `
        --trusted-root $Root --expected-version $Resolved `
        --expected-requested-version $Requested | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$Name policy-bound source install failed" }
    & $Python $lifecycle validate-source --root $Root --manifest $ArtifactManifest `
        --cache $DependencyCache --artifact-id $ArtifactId --destination $vendor `
        --trusted-root $Root --expected-version $Resolved `
        --expected-requested-version $Requested | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$Name source identity validation failed" }
    Write-Host "==> $Name policy-bound vendor tree installed ($Resolved)"
    return $vendor
}

$cc = Join-Path $Root "engines\claude-code\home\skills"
$oc = Join-Path $Root "engines\opencode\xdg\opencode\skill"
New-Item -ItemType Directory -Force -Path $cc, $oc | Out-Null

$sp = Get-Vendored "superpowers" "source-superpowers" `
    $pins['SUPERPOWERS_PIN'] $pins['SUPERPOWERS_COMMIT']
$gs = Get-Vendored "gstack" "source-gstack" `
    $pins['GSTACK_PIN'] $pins['GSTACK_COMMIT']
$VendorRoot = Join-Path $PSScriptRoot "vendor"
$AuditText = (& $Python $Serving skill-policy --vendor $VendorRoot `
    --policy $OfflinePolicy --format json 2>&1 | Out-String).Trim()
$AuditExit = $LASTEXITCODE
if ($AuditExit -notin @(0, 2)) {
    throw "third-party skill policy inspection failed: $AuditText"
}
$Audit = $AuditText | ConvertFrom-Json
foreach ($Flagged in @($Audit.flagged)) {
    Write-Warning ("excluded {0}: {1}" -f $Flagged.name, $Flagged.reason)
}
foreach ($DestinationRoot in @($cc, $oc)) {
    Get-ChildItem -LiteralPath $DestinationRoot -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "sp-*" -or $_.Name -like "gs-*" } |
        Remove-Item -Recurse -Force
}
$Count = 0
foreach ($Allowed in @($Audit.allowed)) {
    $Source = [IO.Path]::GetFullPath([string]$Allowed.path)
    if (-not (Test-Path -LiteralPath (Join-Path $Source "SKILL.md") -PathType Leaf)) {
        throw "allowed skill path is incomplete: $Source"
    }
    foreach ($DestinationRoot in @($cc, $oc)) {
        $Link = Join-Path $DestinationRoot ([string]$Allowed.name)
        if (Test-Path -LiteralPath $Link) {
            Remove-Item -LiteralPath $Link -Recurse -Force
        }
        try {
            New-Item -ItemType Junction -Path $Link -Target $Source | Out-Null
        } catch {
            Copy-Item -LiteralPath $Source -Destination $Link -Recurse -Force
        }
    }
    $Count++
}
Write-Host "==> offline-curated skills synced: $Count"
Write-Host "Flagged network-capable instructions remain in vendor quarantine only."
