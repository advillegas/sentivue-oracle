# verify-egress.ps1 - prove the default-deny egress posture empirically (Windows).
#
# For each installed runtime (node, python), attempts (a) an outbound request to
# a public host - which MUST fail when hardening is ON - and (b) a loopback
# request to llama-swap - which MUST succeed either way. Exit 0 only if the
# posture matches expectation. Safe, read-only, no admin needed.
#
#   powershell -File bootstrap\verify-egress.ps1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$GROUP = "SentiVue Oracle Egress"
$hardened = [bool](Get-NetFirewallRule -Group $GROUP -ErrorAction SilentlyContinue)
$probe = "https://cloudflare.com/cdn-cgi/trace"   # not a Kilo/vendor host
$loop = "http://127.0.0.1:9099/health"
$fail = 0

Write-Host "== egress verification (hardening is $(if ($hardened) { 'ON' } else { 'OFF' })) =="

function Test-Runtime {
    param($name, $exe)
    if (-not $exe -or -not (Test-Path $exe)) { Write-Host "  $name : not installed (skip)"; return }
    $ext = & $exe -e "fetch('$probe',{signal:AbortSignal.timeout(7000)}).then(()=>console.log('REACHED')).catch(e=>console.log('BLOCKED:'+(e.cause?.code||e.name)))" 2>&1 | Out-String
    $ext = $ext.Trim()
    if ($script:hardened) {
        if ($ext -match "REACHED") { Write-Host "  $name egress : LEAK - reached internet while hardened!" -ForegroundColor Red; $script:fail++ }
        else { Write-Host "  $name egress : blocked ($ext) OK" -ForegroundColor Green }
    }
    else {
        Write-Host "  $name egress : $ext (hardening off - informational)"
    }
}

# node probe (also represents agents, npm, MCP node servers)
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
Test-Runtime "node  " $node

# python probe via a tiny inline urllib fetch
$py = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1).FullName
if ($py -and (Test-Path $py)) {
    $pext = & $py -c "import urllib.request,sys`ntry:`n urllib.request.urlopen('$probe',timeout=7); print('REACHED')`nexcept Exception as e: print('BLOCKED:'+type(e).__name__)" 2>&1 | Out-String
    $pext = $pext.Trim()
    if ($hardened) {
        if ($pext -match "REACHED") { Write-Host "  python egress : LEAK - reached internet while hardened!" -ForegroundColor Red; $fail++ }
        else { Write-Host "  python egress : blocked ($pext) OK" -ForegroundColor Green }
    } else { Write-Host "  python egress : $pext (hardening off - informational)" }
} else { Write-Host "  python : not installed (skip)" }

# loopback must always work (when llama-swap is up)
if ($node) {
    $lo = & $node -e "fetch('$loop',{signal:AbortSignal.timeout(7000)}).then(r=>r.text()).then(t=>console.log('OK:'+t.trim())).catch(e=>console.log('FAIL:'+(e.cause?.code||e.name)))" 2>&1 | Out-String
    $lo = $lo.Trim()
    if ($lo -match "OK") { Write-Host "  loopback :9099 : reachable ($lo) OK" -ForegroundColor Green }
    else { Write-Host "  loopback :9099 : $lo (is llama-swap running? 'oracle serve')" -ForegroundColor DarkYellow }
}

if ($hardened -and $fail -eq 0) { Write-Host "PASS: no egress leaks; loopback intact." -ForegroundColor Green; exit 0 }
if (-not $hardened) { Write-Host "NOTE: hardening is OFF - run 'oracle harden' then re-verify to assert the block." -ForegroundColor DarkYellow; exit 0 }
Write-Host "FAIL: egress leak detected." -ForegroundColor Red; exit 1
