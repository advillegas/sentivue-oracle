# doctor.ps1 - Windows twin of doctor.sh: full diagnostic, read-only, safe anytime.
# One verdict line per subsystem; exit 0 unless something is critically broken.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$script:Pass = 0; $script:Fail = 0; $script:Warn = 0

function OK($msg)   { Write-Host (" PASS  {0}" -f $msg); $script:Pass++ }
function BAD($msg, $fix) { Write-Host (" FAIL  {0}" -f $msg); Write-Host ("       fix: {0}" -f $fix); $script:Fail++ }
function MEH($msg, $note) { Write-Host (" WARN  {0}" -f $msg); Write-Host ("       {0}" -f $note); $script:Warn++ }

Write-Host "== system =="
$freeGB = [math]::Round((Get-PSDrive ($Root.Substring(0, 1))).Free / 1GB)
if ($freeGB -gt 50) { OK "disk free: $freeGB GB" } else { MEH "disk free: $freeGB GB" "models + artifacts need headroom" }
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
OK "RAM: $ramGB GB"

Write-Host "== toolbelt =="
$python = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd -and $cmd.Source -notmatch "WindowsApps") { $python = $cmd.Source }
if (-not $python) {
    $c = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($c) { $python = $c.FullName }
}
if ($python) { OK "python: $python" } else { BAD "real python missing (Store stub does not count)" "provision the VERSIONS.lock Python trust root" }
if ($python) {
    & $python -m pytest --version *> $null
    if ($LASTEXITCODE -eq 0) { OK "pytest importable" } else { MEH "pytest missing" "required only for source verification" }
}
$cachedNode = Get-ChildItem (Join-Path $Root ".tools\node") -Recurse -Filter "node.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($cachedNode) { OK "cached node: $($cachedNode.FullName)" } else { BAD "cached node missing" "run bin\oracle.ps1 setup with the policy-bound cache" }
foreach ($eng in @("claude.cmd", "opencode.cmd", "kilo.cmd")) {
    if (Test-Path ".tools\npm\$eng") { OK "engine: $eng" } else { BAD "engine missing: $eng" "bin\oracle.ps1 setup" }
}

Write-Host "== lifecycle =="
if ($python) {
    $lockedPythonLine = Get-Content (Join-Path $Root "VERSIONS.lock") |
        Where-Object { $_ -match "^PYTHON_VERSION=" } | Select-Object -First 1
    $lockedPython = (($lockedPythonLine -split "=", 2)[1] -split "#")[0].Trim()
    $actualPython = (& $python -c "import platform; print(platform.python_version())").Trim()
    if ($LASTEXITCODE -eq 0 -and $actualPython -eq $lockedPython) {
        OK "bootstrap Python trust root matches $lockedPython"
    } else {
        BAD "bootstrap Python is $actualPython, expected $lockedPython" "provision the pinned Python runtime"
    }
    $lifecycle = Join-Path $Root "verification\lifecycle.py"
    & $python $lifecycle validate-dependencies --root $Root *> $null
    if ($LASTEXITCODE -eq 0) {
        OK "dependency pin policy valid"
    } else {
        BAD "dependency pin policy invalid" "$python verification\lifecycle.py validate-dependencies --root `"$Root`""
    }
    $cache = if ($env:ORACLE_DEPENDENCY_CACHE) {
        $env:ORACLE_DEPENDENCY_CACHE
    } else {
        Join-Path $Root "incoming\dependency-cache"
    }
    $manifest = Join-Path $cache "manifest.json"
    if (Test-Path -LiteralPath $manifest) {
        & $python $lifecycle validate-dependencies --root $Root --manifest $manifest --cache $cache --reproducible *> $null
        if ($LASTEXITCODE -eq 0) {
            OK "dependency-cache is policy-bound and reproducible"
        } else {
            BAD "dependency-cache validation failed" "re-export dependencies with bootstrap\export-dependencies.ps1"
        }
        & $python $lifecycle validate-dependencies --root $Root --manifest $manifest `
            --cache $cache --reproducible --include-optional *> $null
        if ($LASTEXITCODE -eq 0) {
            OK "optional dependency exports are also resolved"
        } else {
            MEH "optional dependency exports remain unresolved" `
                "import only the optional components needed on this platform"
        }
    } else {
        MEH "dependency-cache manifest missing" "export online artifacts before reproducible/offline install"
    }
} else {
    BAD "lifecycle checks need pinned Python" "provision the VERSIONS.lock Python trust root"
}
$statePath = Join-Path $Root ".install-state\state.json"
if (Test-Path -LiteralPath $statePath) {
    try {
        $installState = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
        if ($installState.schema_version -eq 1 -and
            $installState.input_sha256 -match "^[0-9a-f]{64}$" -and
            $installState.installation_root -eq $Root) {
            OK "install state records input hash and ownership"
        } else {
            BAD "install state fields are invalid" "re-run bin\oracle.ps1 setup"
        }
    } catch {
        BAD "install state is unreadable" "re-run bin\oracle.ps1 setup"
    }
} else {
    MEH "install state missing" "run bin\oracle.ps1 setup"
}

Write-Host "== platform scopes =="
$PolicyPath = Join-Path $Root "verification\policy.json"
if (Test-Path $PolicyPath) {
    try {
        $Policy = Get-Content -Raw -LiteralPath $PolicyPath | ConvertFrom-Json
        $Scopes = @($Policy.platform_scoped)
        if ($Scopes.Count -eq 0) {
            BAD "platform scope policy is empty" "restore verification\policy.json"
        } else {
            foreach ($Scope in $Scopes) {
                OK ("platform scope: {0} [{1}] - {2}" -f $Scope.path, $Scope.platform, $Scope.reason)
            }
        }
    } catch {
        BAD "platform scope policy is invalid: $($_.Exception.Message)" "restore verification\policy.json"
    }
} else {
    BAD "platform scope policy missing" "restore verification\policy.json"
}

Write-Host "== serving =="
$up = $false
try { Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null; $up = $true } catch {}
if ($up) { OK "llama-swap healthy (http://127.0.0.1:9099)" }
else { MEH "llama-swap DOWN" "start with: bin\oracle.ps1 serve" }
if (Test-Path "serving\tiers.env") {
    foreach ($line in Get-Content "serving\tiers.env") {
        $kv = $line -split "=", 2
        if ($kv.Count -ne 2 -or -not $kv[1].Trim()) { continue }
        $name = $kv[1].Trim()
        $gguf = Get-ChildItem "models\$name" -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($gguf) { OK "tier $($kv[0].Trim()) -> $name (on disk)" }
        else { BAD "tier $($kv[0].Trim()) -> $name has no gguf" "bin\oracle.ps1 models; then connectors\ide\sync-models.ps1" }
    }
} else { MEH "serving\tiers.env missing" "run connectors\ide\sync-models.ps1" }

Write-Host "== engines config =="
$ccSkills = (Get-ChildItem "engines\claude-code\home\skills" -ErrorAction SilentlyContinue | Measure-Object).Count
$ocSkills = (Get-ChildItem "engines\opencode\xdg\opencode\skill" -ErrorAction SilentlyContinue | Measure-Object).Count
if ($ccSkills -gt 0 -and $ocSkills -gt 0) { OK "skills synced (claude: $ccSkills, opencode: $ocSkills)" }
else { BAD "skills not synced (claude: $ccSkills, opencode: $ocSkills)" "bootstrap\sync-skills.ps1 + harness\skill-packs\install-skill-packs.ps1" }
if (Select-String -Path "engines\claude-code\home\settings.json" -Pattern "DISABLE_TELEMETRY" -Quiet) {
    OK "claude telemetry disabled"
} else { BAD "claude telemetry env missing" "restore engines\claude-code\home\settings.json" }

Write-Host "== security posture =="
if (Test-Path "engines\kilo\hardened-env.ps1") { OK "Kilo hardening profile present" } else { BAD "Kilo hardening profile missing" "restore engines\kilo\hardened-env.ps1" }
if (Get-NetFirewallRule -Group "SentiVue Oracle Egress" -ErrorAction SilentlyContinue) { OK "egress default-deny ACTIVE" }
else { MEH "egress default-deny inactive (opt-in)" "bin\oracle.ps1 harden" }
$kiloCfg = Join-Path $Root "state\generated\kilo\kilo.jsonc"
if ((Test-Path $kiloCfg) -and (Select-String -Path $kiloCfg -Pattern 'app\.kilo\.ai' -Quiet)) { BAD "generated kilo.jsonc calls app.kilo.ai" "connectors\ide\sync-models.ps1" }
elseif (Test-Path $kiloCfg) { OK "generated kilo.jsonc has no cloud references" }

Write-Host "== git vault =="
$vault = git remote get-url vault 2>$null
if ($vault) {
    OK "vault remote -> $vault"
    $behind = git rev-list --count vault/main..main 2>$null
    if ($behind -eq "0") { OK "vault main is current" }
    else { MEH "vault main behind by $behind commit(s)" "bin\oracle.ps1 vault sync" }
} else { MEH "no vault remote configured" "bin\oracle.ps1 vault init" }

Write-Host ""
Write-Host ("doctor: {0} pass, {1} warn, {2} fail" -f $script:Pass, $script:Warn, $script:Fail)
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
