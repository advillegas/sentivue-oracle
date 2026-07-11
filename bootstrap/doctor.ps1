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
if ($python) { OK "python: $python" } else { BAD "real python missing (Store stub does not count)" "bootstrap\ensure-tools.ps1" }
if ($python) {
    & $python -m pytest --version *> $null
    if ($LASTEXITCODE -eq 0) { OK "pytest importable" } else { BAD "pytest missing" "bootstrap\ensure-tools.ps1" }
}
if (Get-Command node -ErrorAction SilentlyContinue) { OK "node: $(node --version)" } else { BAD "node missing" "bootstrap\ensure-tools.ps1" }
foreach ($eng in @("claude.cmd", "opencode.cmd", "kilo.cmd")) {
    if (Test-Path ".tools\npm\$eng") { OK "engine: $eng" } else { BAD "engine missing: $eng" "bin\oracle.ps1 setup" }
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
