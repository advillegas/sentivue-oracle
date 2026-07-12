# harden-egress.ps1 - per-process DEFAULT-DENY outbound egress (Windows).
#
# Applies Windows Firewall outbound BLOCK rules to every process class the
# appliance runs: the editor, extension hosts, agent engines, inference servers,
# agent-spawned package managers / MCP servers, and container backends. This is
# the iron-clad layer of the platform's privacy posture: even if an engine or a
# vendored binary ignores its telemetry/gateway kill-switches, its packets
# cannot leave the machine.
#
# Loopback is UNAFFECTED: Windows exempts 127.0.0.0/8 and ::1 from firewall
# filtering, so llama-swap, llama-server, Supabase, and the console keep working
# on 127.0.0.1 while the public internet is denied. There is nothing to
# "allow" - blocking a program's outbound traffic already leaves loopback up.
#
#   powershell -File bootstrap\harden-egress.ps1 on       enable default-deny (needs admin)
#   powershell -File bootstrap\harden-egress.ps1 off       remove the rules (needs admin)
#   powershell -File bootstrap\harden-egress.ps1 status    list rules + resolved targets
#   powershell -File bootstrap\harden-egress.ps1 plan      print resolved program paths only
#
# The envoy network window drops these rules, fetches, and restores them - the
# same toggle model as the macOS pf air-gap.
param([Parameter(Position = 0)][ValidateSet("on", "off", "status", "plan")][string]$Action = "status")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$GROUP = "SentiVue Oracle Egress"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-Targets {
    # class -> list of program image paths that exist on this machine. Every
    # class the request enumerates reduces to one of these images.
    $t = [ordered]@{}
    $add = {
        param($name, $paths)
        $found = @($paths | Where-Object { $_ -and (Test-Path $_) } | Sort-Object -Unique)
        if ($found.Count) { $t[$name] = $found }
    }

    # 1. VSCodium (main + extension host + renderer are all this one image)
    & $add "vscodium" @(
        "$env:LOCALAPPDATA\Programs\VSCodium\VSCodium.exe",
        "$env:ProgramFiles\VSCodium\VSCodium.exe",
        "${env:ProgramFiles(x86)}\VSCodium\VSCodium.exe")

    # 2. Node.js - agent engines (claude/opencode), MCP node servers, npm/npx
    #    invoked by agents, and the extension host's node children
    $nodePaths = @()
    $nc = Get-Command node -ErrorAction SilentlyContinue
    if ($nc) { $nodePaths += $nc.Source }
    $nodePaths += @("$env:ProgramFiles\nodejs\node.exe", "$env:LOCALAPPDATA\Programs\nodejs\node.exe")
    & $add "node" $nodePaths

    # 3. Kilo - standalone compiled Bun binary (not node)
    & $add "kilo" @(
        (Join-Path $Root ".tools\npm\node_modules\@kilocode\cli\node_modules\@kilocode\cli-windows-x64\bin\kilo.exe"),
        (Join-Path $Root ".tools\npm\node_modules\@kilocode\cli\node_modules\@kilocode\cli-windows-x64-baseline\bin\kilo.exe"),
        (Join-Path $Root ".tools\npm\node_modules\@kilocode\cli\bin\.kilo"))

    # 4. Inference servers
    & $add "inference" @(
        (Join-Path $Root ".tools\win\llama\llama-server.exe"),
        (Join-Path $Root ".tools\win\llama-swap.exe"),
        (Join-Path $Root ".tools\bin\llama-swap.exe"))

    # 5. Python - conductor, python agents, and pip invoked by agents
    $pyPaths = @()
    $pc = Get-Command python -ErrorAction SilentlyContinue
    if ($pc -and $pc.Source -notmatch "WindowsApps") { $pyPaths += $pc.Source }
    $pyPaths += @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $pyPaths += @(Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    & $add "python" $pyPaths

    # 6. uv / uvx - python package manager + uvx-launched MCP servers
    $uvPaths = @()
    $uc = Get-Command uv -ErrorAction SilentlyContinue; if ($uc) { $uvPaths += $uc.Source }
    $uxc = Get-Command uvx -ErrorAction SilentlyContinue; if ($uxc) { $uvPaths += $uxc.Source }
    $uvPaths += @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.local\bin\uvx.exe")
    & $add "uv" $uvPaths

    # 7. Container backends launched by agents (best-effort: Docker's own daemon
    #    egress also needs the daemon's config; these cover the host-side procs)
    & $add "containers" @(
        "$env:ProgramFiles\Docker\Docker\resources\com.docker.backend.exe",
        "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
        "$env:ProgramFiles\Docker\Docker\resources\dockerd.exe")

    return $t
}

function Show-Plan {
    $t = Resolve-Targets
    Write-Host "== egress default-deny plan (loopback stays reachable) =="
    foreach ($k in $t.Keys) {
        Write-Host ("  {0,-11} {1} path(s)" -f $k, $t[$k].Count)
        foreach ($p in $t[$k]) { Write-Host "      $p" -ForegroundColor DarkGray }
    }
    $missing = @("vscodium", "node", "kilo", "inference", "python") | Where-Object { -not $t.Contains($_) }
    if ($missing) { Write-Host "  (not present yet: $($missing -join ', '))" -ForegroundColor DarkYellow }
}

switch ($Action) {
    "plan" { Show-Plan; break }
    "status" {
        $rules = Get-NetFirewallRule -Group $GROUP -ErrorAction SilentlyContinue
        if ($rules) {
            Write-Host "egress default-deny ACTIVE - $($rules.Count) block rule(s):"
            foreach ($r in $rules) {
                $app = ($r | Get-NetFirewallApplicationFilter).Program
                Write-Host ("  [{0}] {1}" -f $r.Enabled, $app)
            }
        }
        else { Write-Host "egress default-deny INACTIVE (no '$GROUP' rules). Enable: oracle harden" }
        Write-Host ""; Show-Plan; break
    }
    "on" {
        if (-not (Test-Admin)) {
            Write-Host "==> egress hardening needs admin - relaunching elevated (accept the UAC prompt)"
            Start-Process powershell -Verb RunAs -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "on") | Out-Null
            return
        }
        Get-NetFirewallRule -Group $GROUP -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
        $t = Resolve-Targets
        $count = 0
        foreach ($k in $t.Keys) {
            $i = 0
            foreach ($p in $t[$k]) {
                $i++
                New-NetFirewallRule -DisplayName "SentiVue Oracle egress-deny: $k$(if ($t[$k].Count -gt 1) { " ($i)" })" `
                    -Group $GROUP -Direction Outbound -Action Block -Program $p `
                    -Profile Any -Enabled True -ErrorAction Stop | Out-Null
                $count++
            }
        }
        Write-Host "Egress default-deny ACTIVE: $count program rule(s). Loopback (127.0.0.1) still works."
        Write-Host "Local models, Supabase, and the console are unaffected; the public internet is denied."
        Write-Host "Undo with: powershell -File bootstrap\harden-egress.ps1 off   (or: oracle harden off)"
        break
    }
    "off" {
        if (-not (Test-Admin)) {
            Write-Host "==> removing egress rules needs admin - relaunching elevated"
            Start-Process powershell -Verb RunAs -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "off") | Out-Null
            return
        }
        $rules = Get-NetFirewallRule -Group $GROUP -ErrorAction SilentlyContinue
        if ($rules) { $rules | Remove-NetFirewallRule; Write-Host "Egress rules removed. Outbound restored." }
        else { Write-Host "No '$GROUP' rules to remove." }
        break
    }
}
