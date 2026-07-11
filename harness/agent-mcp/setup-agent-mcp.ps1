# setup-agent-mcp.ps1 - Agent-MCP orchestration viewer (OPTIONAL component).
# Multi-agent coordination server + live dashboard: WATCH how agents are
# orchestrated - agents, tasks, and shared context as a living graph.
# Fully local: the server is pointed at llama-swap (embeddings included via
# the text-embedding-3-large alias) and bound to 127.0.0.1.
#
#   powershell -File harness\agent-mcp\setup-agent-mcp.ps1 install
#   ... start | stop | status
param([Parameter(Position = 0)][string]$Cmd = "status")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Vendor = Join-Path $PSScriptRoot "vendor"
$StateDir = Join-Path $Root "state"
$LogDir = Join-Path $Root "logs"
$SrvPid = Join-Path $StateDir "agent-mcp.pid"
$DashPid = Join-Path $StateDir "agent-mcp-dash.pid"
$Port = 8100
$DashPort = 3847

$pins = @{}
Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" } | ForEach-Object {
    $kv = $_ -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim()
}

function Set-LocalEnv {
    # everything rides llama-swap; nothing leaves the machine
    $env:OPENAI_API_KEY = "oracle-local"
    $env:OPENAI_BASE_URL = "http://127.0.0.1:9099/v1"
    $env:AGENT_MCP_HOST = "127.0.0.1"
    $env:AGENT_MCP_PORT = "$Port"
    $env:AGENT_MCP_PROJECT_DIR = $Root
    # the server prints box-drawing chars; Windows cp1252 consoles choke without UTF-8
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:NEXT_TELEMETRY_DISABLED = "1"
}

function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe",
                     "$env:USERPROFILE\.local\bin\uv.exe",
                     "$env:LOCALAPPDATA\Programs\uv\uv.exe")) {
        if (Test-Path $p) { return $p }
    }
    $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "uv.exe" -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pkg) { return $pkg.FullName }
    return $null
}

switch ($Cmd) {
    "install" {
        $uv = Find-Uv
        if (-not $uv) {
            Write-Host "==> uv missing - installing (winget)"
            winget install --id astral-sh.uv -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
            $uv = Find-Uv
            if (-not $uv) { Write-Host "ERROR: uv installed but not found - open a fresh terminal and re-run"; exit 1 }
        }
        if (-not (Test-Path (Join-Path $Vendor ".git"))) {
            Write-Host "==> cloning Agent-MCP $($pins['AGENT_MCP_PIN']) (shallow, pinned)"
            git clone --depth 1 --branch $pins['AGENT_MCP_PIN'] $pins['AGENT_MCP_REPO'] $Vendor
        } else {
            Write-Host "==> Agent-MCP vendor checkout present"
        }
        Push-Location $Vendor
        Write-Host "==> python env (uv sync via $uv)"
        # uv logs to stderr; PS 5.1 + EAP Stop would turn that into a fake error
        $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        & $uv sync 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { & $uv venv 2>&1 | Out-Host; & $uv pip install -e . 2>&1 | Out-Host }
        # upstream uses Starlette's on_startup kwarg (removed in 0.47) but pins loosely
        & $uv pip install "starlette<0.47" 2>&1 | Out-Host
        $ErrorActionPreference = $eap
        Pop-Location
        $dash = Join-Path $Vendor "agent_mcp\dashboard"
        if (Test-Path (Join-Path $dash "package.json")) {
            Write-Host "==> dashboard deps (npm install)"
            Push-Location $dash
            npm install --no-audit --no-fund | Out-Null
            Pop-Location
        }
        Write-Host "==> Agent-MCP installed. Start the viewer with: bin\oracle.ps1 agents-ui"
    }
    "start" {
        if (-not (Test-Path $Vendor)) { Write-Host "not installed - run: harness\agent-mcp\setup-agent-mcp.ps1 install"; exit 1 }
        $condLock = Join-Path $StateDir "conductor.lock"
        if (Test-Path $condLock) {
            Write-Host "WARNING: a mission is running (state\conductor.lock). On shared-CPU"
            Write-Host "         hardware the viewer's model calls compete with engine inference"
            Write-Host "         and can starve the mission into watchdog kills."
        }
        $uv = Find-Uv
        if (-not $uv) { Write-Host "ERROR: uv not found - run install first"; exit 1 }
        New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null
        Set-LocalEnv
        # --no-index: the auto-RAG indexer floods the local embedding slot with
        # multi-minute batches and starves engine inference on shared hardware.
        # Index selectively via its RAG tools when you actually need retrieval.
        $p = Start-Process -FilePath $uv -ArgumentList "run", "--no-sync", "-m", "agent_mcp.cli", `
            "--port", "$Port", "--project-dir", $Root, "--no-tui", "--no-index" `
            -WorkingDirectory $Vendor -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $LogDir "agent-mcp.out.log") `
            -RedirectStandardError (Join-Path $LogDir "agent-mcp.err.log")
        Set-Content $SrvPid $p.Id
        $dash = Join-Path $Vendor "agent_mcp\dashboard"
        if (Test-Path (Join-Path $dash "package.json")) {
            # bypass upstream's dev wrapper (spawns bare 'npx' = ENOENT on Windows,
            # binds 0.0.0.0); run next directly, loopback only
            $d = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", `
                "npx next dev --port $DashPort --hostname 127.0.0.1" `
                -WorkingDirectory $dash -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $LogDir "agent-mcp-dash.out.log") `
                -RedirectStandardError (Join-Path $LogDir "agent-mcp-dash.err.log")
            Set-Content $DashPid $d.Id
        }
        Write-Host "Agent-MCP server:    http://127.0.0.1:$Port  (MCP endpoint /mcp)"
        Write-Host "Orchestration view:  http://127.0.0.1:$DashPort  (give it ~20s to compile)"
    }
    "stop" {
        foreach ($f in @($SrvPid, $DashPid)) {
            if (Test-Path $f) {
                $procId = Get-Content $f
                & taskkill /F /T /PID $procId 2>$null | Out-Null
                Remove-Item $f -Force
            }
        }
        Write-Host "agent-mcp stopped"
    }
    "status" {
        foreach ($probe in @(@("server", $Port), @("viewer", $DashPort))) {
            $name, $p = $probe
            $tcp = New-Object Net.Sockets.TcpClient
            try {
                $ok = $tcp.ConnectAsync("127.0.0.1", $p).Wait(2000)
                Write-Host "$($name): $(if ($ok) { "UP (http://127.0.0.1:$p)" } else { 'DOWN' })"
            } catch { Write-Host "$($name): DOWN" } finally { $tcp.Dispose() }
        }
    }
    default { Write-Host "usage: setup-agent-mcp.ps1 {install|start|stop|status}" }
}
