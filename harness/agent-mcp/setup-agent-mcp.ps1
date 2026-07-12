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
    $env:UV_OFFLINE = "1"
    $env:UV_CACHE_DIR = Join-Path $DependencyCache "uv"
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

function Assert-ValidatedVendor {
    & $Python (Join-Path $Root "verification\lifecycle.py") validate-source `
        --root $Root --manifest $ArtifactManifest --cache $DependencyCache `
        --artifact-id "source-agent-mcp" --destination $Vendor --trusted-root $Root `
        --expected-version $pins['AGENT_MCP_COMMIT'] `
        --expected-requested-version $pins['AGENT_MCP_PIN'] | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Agent-MCP source identity validation failed" }
}

switch ($Cmd) {
    "install" {
        $uv = Find-Uv
        if (-not $uv) { throw "uv is missing from the validated offline toolchain" }
        & $Python (Join-Path $Root "verification\lifecycle.py") preflight-source `
            --root $Root --manifest $ArtifactManifest --cache $DependencyCache `
            --artifact-id "source-agent-mcp" --destination $Vendor --trusted-root $Root `
            --expected-version $pins['AGENT_MCP_COMMIT'] `
            --expected-requested-version $pins['AGENT_MCP_PIN'] | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Agent-MCP source install preflight failed" }
        & $Python (Join-Path $Root "verification\lifecycle.py") install-source `
            --root $Root --manifest $ArtifactManifest --cache $DependencyCache `
            --artifact-id "source-agent-mcp" --destination $Vendor --trusted-root $Root `
            --expected-version $pins['AGENT_MCP_COMMIT'] `
            --expected-requested-version $pins['AGENT_MCP_PIN'] | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Agent-MCP policy-bound source install failed" }
        Assert-ValidatedVendor
        Set-LocalEnv
        Push-Location $Vendor
        Write-Host "==> python env (offline frozen uv sync via $uv)"
        # uv logs to stderr; PS 5.1 + EAP Stop would turn that into a fake error
        $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        & $uv sync --offline --frozen 2>&1 | Out-Host
        $syncExit = $LASTEXITCODE
        $ErrorActionPreference = $eap
        Pop-Location
        if ($syncExit -ne 0) { throw "Agent-MCP offline locked environment is incomplete" }
        $dash = Join-Path $Vendor "agent_mcp\dashboard"
        if (Test-Path (Join-Path $dash "package.json")) {
            if (-not (Test-Path (Join-Path $dash "package-lock.json"))) {
                throw "validated Agent-MCP export has no dashboard lock"
            }
            Write-Host "==> dashboard deps (offline lock install)"
            Push-Location $dash
            npm ci --offline --ignore-scripts --no-audit --no-fund | Out-Null
            $npmExit = $LASTEXITCODE
            Pop-Location
            if ($npmExit -ne 0) { throw "offline dashboard dependency install failed" }
        }
        Write-Host "==> Agent-MCP installed. Start the viewer with: bin\oracle.ps1 agents-ui"
    }
    "start" {
        Assert-ValidatedVendor
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
            $next = Join-Path $dash "node_modules\.bin\next.cmd"
            if (-not (Test-Path -LiteralPath $next)) {
                throw "offline dashboard runtime is missing"
            }
            $d = Start-Process -FilePath $next -ArgumentList `
                "dev", "--port", "$DashPort", "--hostname", "127.0.0.1" `
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
