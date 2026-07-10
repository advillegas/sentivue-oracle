# ensure-tools.ps1 - self-provisioning toolbelt healer (Windows).
# Doctrine: a missing tool is a task, not a blocker. This script is idempotent,
# best-effort, and safe to run any time; launchers and the conductor call it
# automatically when they hit a missing dependency.
#
#   powershell -File bootstrap\ensure-tools.ps1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$fixed = @()

function Find-RealPython {
    # the Microsoft Store stub does not count
    $cands = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps") { $cands += $cmd.Source }
    $cands += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    $cands += Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    return $null
}

# ---- 1. real Python ----------------------------------------------------------
$python = Find-RealPython
if (-not $python) {
    Write-Host "==> python missing - installing Python 3.12 (winget)"
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $python = Find-RealPython
    if ($python) { $fixed += "python" }
}
if ($python) {
    # make 'python' resolve ahead of the WindowsApps stub for this and future processes
    $pyDir = Split-Path -Parent $python
    $scripts = Join-Path $pyDir "Scripts"
    foreach ($scope in @("process", "user")) {
        $cur = if ($scope -eq "process") { $env:PATH } else { [Environment]::GetEnvironmentVariable("Path", "User") }
        if ($cur -notlike "$pyDir*") {
            $new = "$pyDir;$scripts;$cur"
            if ($scope -eq "process") { $env:PATH = $new }
            else { [Environment]::SetEnvironmentVariable("Path", $new, "User"); $fixed += "PATH(user)" }
        }
    }
}

# ---- 2. pytest (test runner for missions' deterministic checks) ---------------
if ($python) {
    & $python -m pytest --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "==> pytest missing - installing (pip, pinned to major 8)"
        & $python -m pip install --quiet "pytest>=8,<9"
        if ($LASTEXITCODE -eq 0) { $fixed += "pytest" }
    }
}

# ---- 3. node (engines are npm packages) ---------------------------------------
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "==> node missing - installing Node LTS (winget)"
    winget install --id OpenJS.NodeJS.LTS -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $fixed += "node"
}

# ---- 4. engines (pinned, repo-local npm prefix) --------------------------------
$claudeOk = Test-Path (Join-Path $Root ".tools\npm\claude.cmd")
$openOk = Test-Path (Join-Path $Root ".tools\npm\opencode.cmd")
if ((-not $claudeOk -or -not $openOk) -and (Get-Command npm -ErrorAction SilentlyContinue)) {
    $pins = @{}
    Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" } | ForEach-Object {
        $kv = $_ -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim()
    }
    Write-Host "==> engines missing - npm install (pinned, repo-local)"
    $env:npm_config_prefix = Join-Path $Root ".tools\npm"
    New-Item -ItemType Directory -Force -Path $env:npm_config_prefix | Out-Null
    npm install -g "@anthropic-ai/claude-code@$($pins['CLAUDE_CODE_NPM_VERSION'])" "opencode-ai@$($pins['OPENCODE_NPM_VERSION'])" | Out-Null
    if ($LASTEXITCODE -eq 0) { $fixed += "engines" }
}

# ---- 5. git -------------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "==> git missing - installing (winget)"
    winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $fixed += "git"
}

$pyV = if ($python) { (& $python --version) -replace "Python ", "" } else { "MISSING" }
$state = "TOOLS: python $pyV | pytest $(if ($python) { & $python -m pytest --version 2>$null | Select-Object -First 1 } else { '?' } ) | node $(if (Get-Command node -ErrorAction SilentlyContinue) { node --version } else { 'MISSING' }) | engines $(if (Test-Path (Join-Path $Root '.tools\npm\claude.cmd')) { 'OK' } else { 'MISSING' })"
if ($fixed) { Write-Host "==> self-provisioned: $($fixed -join ', ')" }
Write-Host $state
