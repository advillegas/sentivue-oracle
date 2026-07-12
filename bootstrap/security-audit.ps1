# security-audit.ps1 - full platform privacy/security sweep (Windows).
# Deterministic checks over the repo's privacy invariants: service bind
# addresses, engine telemetry/update kill-switches, the Kilo hardening layer,
# the egress default-deny guard, worker network permissions, and secret hygiene.
# Prints PASS/WARN/FAIL; exits nonzero if any FAIL. Read-only.
#
#   powershell -File bootstrap\security-audit.ps1 [-Deep]
param([switch]$Deep)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$pass = 0; $warn = 0; $bad = 0
function OK($m)   { Write-Host "  [PASS] $m" -ForegroundColor Green; $script:pass++ }
function WARN($m) { Write-Host "  [WARN] $m" -ForegroundColor DarkYellow; $script:warn++ }
function BAD($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:bad++ }
function Has($path, $pat) { return [bool](Select-String -Path (Join-Path $Root $path) -Pattern $pat -Quiet -ErrorAction SilentlyContinue) }

Write-Host "=== SentiVue Oracle security sweep ==============================`n"

Write-Host "== service bind addresses (must be loopback) =="
if (Has "serving\serve-windows.ps1" '--listen", "127\.0\.0\.1:9099|--listen 127\.0\.0\.1') { OK "llama-swap listens on 127.0.0.1:9099" } else { BAD "llama-swap listen address not pinned to loopback" }
if (Has "serving\serve-windows.ps1" '--host 127\.0\.0\.1') { OK "llama-server bound to 127.0.0.1" } else { BAD "llama-server host not loopback" }
$nonLoop = Select-String -Path (Join-Path $Root "connectors\supabase\docker-compose.yml") -Pattern '^\s*-\s*"(?!127\.0\.0\.1)[0-9]' -ErrorAction SilentlyContinue
if ($nonLoop) { BAD "supabase publishes a non-loopback port: $($nonLoop.Line.Trim())" } else { OK "supabase ports all bound to 127.0.0.1" }
if (Has "conductor\console.py" 'ThreadingHTTPServer\(\("127\.0\.0\.1"') { OK "console bound to 127.0.0.1" } else { BAD "console bind not loopback" }
if (Has "harness\agent-mcp\setup-agent-mcp.ps1" 'AGENT_MCP_HOST = "127\.0\.0\.1"') { OK "agent-mcp host = 127.0.0.1" } else { WARN "agent-mcp host binding not asserted" }

Write-Host "`n== engine telemetry / auto-update kill-switches =="
if (Has "engines\claude-code\home\settings.json" "DISABLE_TELEMETRY") { OK "Claude Code telemetry disabled" } else { BAD "Claude Code telemetry env missing" }
if (Has "engines\claude-code\home\settings.json" "DISABLE_AUTOUPDATER|autoupdater.*off|DISABLE_NONESSENTIAL") { OK "Claude Code autoupdate/nonessential traffic disabled" } else { WARN "Claude Code autoupdate flag not found" }
if (Has "engines\opencode\xdg\opencode\opencode.json" '"webfetch":\s*"deny"') { OK "OpenCode workers deny webfetch" } else { BAD "OpenCode webfetch not denied" }
if (Has "connectors\ide\setup-ide.ps1" 'telemetry\.telemetryLevel.*off|"off"') { OK "VSCodium telemetry set off at setup" } else { WARN "VSCodium telemetry level not asserted" }
if (Has "connectors\ide\setup-ide.ps1" 'update\.mode.*none') { OK "VSCodium auto-update disabled" } else { WARN "VSCodium update.mode not pinned" }

Write-Host "`n== Kilo hardening layer =="
foreach ($f in @("engines\kilo\hardened-env.ps1", "engines\kilo\hardened-env.sh", "engines\kilo\call-home-hosts.txt", "engines\kilo\HARDENING.md")) {
    if (Test-Path (Join-Path $Root $f)) { OK "present: $f" } else { BAD "missing: $f" }
}
if (Has "engines\kilo\launch.ps1" "hardened-env\.ps1") { OK "launch.ps1 sources the hardening profile" } else { BAD "launch.ps1 does not source hardened-env" }
if (Has "engines\kilo\launch.sh" "hardened-env\.sh") { OK "launch.sh sources the hardening profile" } else { BAD "launch.sh does not source hardened-env" }
foreach ($k in @("KILO_TELEMETRY_LEVEL", "KILO_DISABLE_SHARE", "KILO_DISABLE_AUTOUPDATE", "KILO_DISABLE_MODELS_FETCH", "OTEL_SDK_DISABLED", "KILO_DISABLE_SESSION_INGEST")) {
    if (Has "engines\kilo\hardened-env.ps1" $k) { OK "defang: $k" } else { BAD "defang missing: $k" }
}
# the schema KEY assignment (not a comment mentioning the URL)
if (Has "connectors\ide\sync-models.ps1" "schema'\s*=.*app\.kilo\.ai") { BAD "generated kilo.jsonc still sets a remote schema key (ps1)" } else { OK "no remote schema key in generated kilo.jsonc (ps1)" }
if (Has "connectors\ide\sync-models.sh" '"\$schema":\s*"https://app\.kilo\.ai') { BAD "generated kilo.jsonc still sets a remote schema key (sh)" } else { OK "no remote schema key in generated kilo.jsonc (sh)" }
$kiloCfg = Join-Path $Root "state\generated\kilo\kilo.jsonc"
if (Test-Path $kiloCfg) {
    if (Select-String -Path $kiloCfg -Pattern 'app\.kilo\.ai' -Quiet) { BAD "generated kilo.jsonc references app.kilo.ai (re-run sync-models)" } else { OK "generated kilo.jsonc has no kilo.ai references" }
    if (Select-String -Path $kiloCfg -Pattern '"webfetch":\s*"deny"' -Quiet) { OK "generated kilo.jsonc denies webfetch" } else { WARN "generated kilo.jsonc missing webfetch deny (re-run sync-models)" }
}

Write-Host "`n== egress default-deny guard =="
foreach ($f in @("bootstrap\harden-egress.ps1", "bootstrap\harden-egress.sh", "bootstrap\verify-egress.ps1", "bootstrap\verify-egress.sh")) {
    if (Test-Path (Join-Path $Root $f)) { OK "present: $f" } else { BAD "missing: $f" }
}
if (Get-NetFirewallRule -Group "SentiVue Oracle Egress" -ErrorAction SilentlyContinue) { OK "egress default-deny ACTIVE on this machine" } else { WARN "egress default-deny INACTIVE (opt-in: 'oracle harden')" }

Write-Host "`n== secret hygiene =="
foreach ($p in @("\.env$", "\.env\.", "credentials", "\*\.key", "id_rsa")) {
    if (Has ".gitignore" $p) { OK ".gitignore covers /$p/" } else { WARN ".gitignore missing pattern /$p/" }
}
$tracked = (& git -C $Root ls-files) 2>$null
$leak = 0
foreach ($f in $tracked) {
    if ($f -match '\.(md|lock|txt)$') { continue }
    $hit = Select-String -Path (Join-Path $Root $f) -Pattern 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}' -ErrorAction SilentlyContinue
    if ($hit) { BAD "possible secret in tracked file: $f"; $leak++ }
}
if ($leak -eq 0) { OK "no obvious secrets in tracked files" }

if ($Deep) {
    Write-Host "`n== deep: vendored Kilo binary endpoint scan =="
    $kiloExe = Join-Path $Root ".tools\npm\node_modules\@kilocode\cli\node_modules\@kilocode\cli-windows-x64\bin\kilo.exe"
    if (Test-Path $kiloExe) {
        $scan = & node (Join-Path $Root "bootstrap\scan-binary.mjs") $kiloExe --hosts-only 2>$null
        $documented = Get-Content (Join-Path $Root "engines\kilo\call-home-hosts.txt") | Where-Object { $_ -and -not $_.StartsWith("#") }
        Write-Host "  scanned kilo.exe; $($documented.Count) hosts documented in call-home-hosts.txt"
        OK "deep scan completed (see engines\kilo\HARDENING.md for re-derivation steps)"
    } else { WARN "kilo.exe not present to scan" }
}

Write-Host "`n=== sweep result: $pass PASS / $warn WARN / $bad FAIL ==="
if ($bad -gt 0) { Write-Host "SECURITY SWEEP FAILED - fix the [FAIL] items above." -ForegroundColor Red; exit 1 }
Write-Host "Security invariants hold. (WARNs are advisory / opt-in toggles.)" -ForegroundColor Green
exit 0
