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

$pins = @{}
Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" } | ForEach-Object {
    $kv = $_ -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim()
}

function Get-Vendored([string]$Name, [string]$Repo, [string]$Pin) {
    $vendor = Join-Path $PSScriptRoot "vendor\$Name"
    if (-not (Test-Path (Join-Path $vendor ".git"))) {
        Write-Host "==> cloning $Name @ $Pin (shallow, pinned)"
        if ($Pin -match "^[0-9a-f]{40}$") {
            git clone --filter=blob:none $Repo $vendor
            git -C $vendor checkout --quiet $Pin
        } else {
            git clone --depth 1 --branch $Pin $Repo $vendor
        }
    } else { Write-Host "==> $Name vendor checkout present" }
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
$sp = Get-Vendored "superpowers" $pins['SUPERPOWERS_REPO'] $pins['SUPERPOWERS_PIN']
$n1 = Sync-SkillDirs $sp "sp" @("skills")
Write-Host "==> superpowers: $n1 skills synced (prefix 'sp-')"

# gstack: each top-level dir with a SKILL.md is a skill
$gs = Get-Vendored "gstack" $pins['GSTACK_REPO'] $pins['GSTACK_PIN']
$n2 = Sync-SkillDirs $gs "gs" @(".")
Write-Host "==> gstack: $n2 skills synced (prefix 'gs-')"
Write-Host "Both packs live in harness\skill-packs\vendor; re-run after changing pins."
