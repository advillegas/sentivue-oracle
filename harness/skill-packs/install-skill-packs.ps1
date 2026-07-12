# install-skill-packs.ps1 - vendor pinned third-party skill frameworks and sync
# them into both engines: obra/superpowers (methodology: brainstorm -> plan ->
# TDD -> two-stage review) and garrytan/gstack (23 role specialists: CEO/eng/
# design reviews, QA, ship, retro). Skills are markdown-only - they run on OUR
# local models through OUR engines; no cloud, no accounts (browser/deploy
# skills that assume external services simply go unused).
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
    & $Python $lifecycle install-source --root $Root --manifest $ArtifactManifest `
        --cache $DependencyCache --artifact-id $ArtifactId --destination $vendor `
        --expected-version $Resolved --expected-requested-version $Requested | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$Name policy-bound source install failed" }
    & $Python $lifecycle validate-source --root $Root --manifest $ArtifactManifest `
        --cache $DependencyCache --artifact-id $ArtifactId --destination $vendor `
        --expected-version $Resolved --expected-requested-version $Requested | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$Name source identity validation failed" }
    Write-Host "==> $Name policy-bound vendor tree installed ($Resolved)"
    return $vendor
}

$cc = Join-Path $Root "engines\claude-code\home\skills"
$oc = Join-Path $Root "engines\opencode\xdg\opencode\skill"
New-Item -ItemType Directory -Force -Path $cc, $oc | Out-Null

function Sync-SkillDirs([string]$Vendor, [string]$Prefix, [string[]]$SkillRoots) {
    $count = 0
    foreach ($rootRel in $SkillRoots) {
        $base = Join-Path $Vendor $rootRel
        if (-not (Test-Path $base)) { continue }
        foreach ($dir in Get-ChildItem $base -Directory) {
            if (-not (Test-Path (Join-Path $dir.FullName "SKILL.md"))) { continue }
            foreach ($dest in @($cc, $oc)) {
                $link = Join-Path $dest "$Prefix-$($dir.Name)"
                if (Test-Path $link) { Remove-Item $link -Recurse -Force }
                try { New-Item -ItemType Junction -Path $link -Target $dir.FullName | Out-Null }
                catch { Copy-Item $dir.FullName $link -Recurse -Force }
            }
            $count++
        }
    }
    return $count
}

# superpowers: skills live under skills/<name>/SKILL.md
$sp = Get-Vendored "superpowers" "source-superpowers" `
    $pins['SUPERPOWERS_PIN'] $pins['SUPERPOWERS_COMMIT']
$n1 = Sync-SkillDirs $sp "sp" @("skills")
Write-Host "==> superpowers: $n1 skills synced (prefix 'sp-')"

# gstack: each top-level dir with a SKILL.md is a skill
$gs = Get-Vendored "gstack" "source-gstack" `
    $pins['GSTACK_PIN'] $pins['GSTACK_COMMIT']
$n2 = Sync-SkillDirs $gs "gs" @(".")
Write-Host "==> gstack: $n2 skills synced (prefix 'gs-')"
Write-Host "Both packs live in harness\skill-packs\vendor; re-run after changing pins."
