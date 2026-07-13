# oracle.ps1 - Windows operator CLI, intentionally aligned with bin/oracle.
param(
    [Parameter(Position = 0)][string]$Cmd = "help",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$env:PATH = (Join-Path $Root "env\.venv\Scripts") + ";" +
    (Join-Path $Root ".tools\bin") + ";" +
    (Join-Path $Root ".tools\npm") + ";" + $env:PATH
$env:ORACLE_ROOT = $Root
$env:ORACLE_PROJECT_ROOT = $Root
. (Join-Path $Root "engines\shared\lean-ctx-env.ps1")
$env:UV_OFFLINE = "1"
$env:UV_CACHE_DIR = Join-Path $Root "incoming\dependency-cache\uv"

function Find-CodiumInstalled {
    return [bool](Get-ChildItem (Join-Path $Root ".tools\vscodium\app") `
        -Recurse -Filter "codium.cmd" -ErrorAction SilentlyContinue |
        Select-Object -First 1)
}

function Find-RealPython {
    $venv = Join-Path $Root "env\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps") { return $cmd.Source }
    $c = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($c) { return $c.FullName }
    return $null
}

function Get-CachedArtifact {
    param(
        [string]$ArtifactId,
        [string]$ExpectedVersion,
        [string]$Python
    )
    $cache = if ($env:ORACLE_DEPENDENCY_CACHE) {
        $env:ORACLE_DEPENDENCY_CACHE
    } else {
        Join-Path $Root "incoming\dependency-cache"
    }
    $manifest = Join-Path $cache "manifest.json"
    $lifecycleArgs = @(
        (Join-Path $Root "verification\lifecycle.py"), "artifact-path",
        "--manifest", $manifest, "--cache", $cache,
        "--artifact-id", $ArtifactId,
        "--expected-requested-version", $ExpectedVersion,
        "--root", $Root, "--reproducible"
    )
    if ($ExpectedVersion -notin @("dynamic", "unresolved")) {
        $lifecycleArgs += @("--expected-version", $ExpectedVersion)
    }
    $path = (& $Python @lifecycleArgs)
    if ($LASTEXITCODE -ne 0) {
        throw "cached artifact validation failed: $ArtifactId"
    }
    return $path.Trim()
}

function Invoke-NativeChecked {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    & $Action
    $code = $LASTEXITCODE
    if ($null -ne $code -and $code -ne 0) {
        throw "$Name failed with exit code $code"
    }
}

function Invoke-Conductor {
    param([string[]]$CondArgs)
    $python = Find-RealPython
    if (-not $python) {
        Write-Host "ERROR: pinned Python is not provisioned"
        exit 1
    }
    $pyDir = Split-Path -Parent $python
    $env:PATH = "$pyDir;$pyDir\Scripts;$env:PATH"
    & $python (Join-Path $Root "conductor\conductor.py") @CondArgs
    exit $LASTEXITCODE
}

switch ($Cmd) {
    "vault"  { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") @Rest }
    "models" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\download-models.ps1") @Rest }
    "uninstall" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\uninstall.ps1") @Rest }
    "finish" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\finish-windows.ps1") @Rest }
    "setup" {
        # Full platform on Windows: engines (pinned npm, repo-local) + serving toolchain.
        $lock = Get-Content (Join-Path $Root "VERSIONS.lock") | Where-Object { $_ -match "=" }
        $pins = @{}
        foreach ($l in $lock) { $kv = $l -split "=", 2; $pins[$kv[0].Trim()] = ($kv[1] -split "#")[0].Trim() }
        $python = Find-RealPython
        if (-not $python) { throw "Python 3.12 is a platform prerequisite for offline cache validation" }
        $actualPython = (& $python -c "import platform; print(platform.python_version())").Trim()
        if ($LASTEXITCODE -ne 0 -or $actualPython -ne $pins["PYTHON_VERSION"]) {
            throw "bootstrap trust root requires Python $($pins['PYTHON_VERSION']), found $actualPython"
        }
        $lifecycle = Join-Path $Root "verification\lifecycle.py"
        Invoke-NativeChecked "state init" {
            & $python $lifecycle state init --root $Root --home $env:USERPROFILE | Out-Null
        }
        & $python $lifecycle state phase-current --root $Root `
            --home $env:USERPROFILE --phase "windows-setup" *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "==> setup already current for this source and dependency export"
            break
        }
        Invoke-NativeChecked "state begin-phase" {
            & $python $lifecycle state begin-phase --root $Root `
                --home $env:USERPROFILE --phase "windows-setup" | Out-Null
        }
        $cache = if ($env:ORACLE_DEPENDENCY_CACHE) {
            $env:ORACLE_DEPENDENCY_CACHE
        } else {
            Join-Path $Root "incoming\dependency-cache"
        }
        $env:npm_config_prefix = Join-Path $Root ".tools\npm"
        $env:npm_config_cache = Join-Path $cache "npm"
        $env:npm_config_offline = "true"
        New-Item -ItemType Directory -Force -Path $env:npm_config_prefix | Out-Null
        $nodeArchive = Get-CachedArtifact "node-windows-x64" $pins["NODE_VERSION"] $python
        $nodeRoot = Join-Path $Root ".tools\node"
        $nodeStage = Join-Path $Root (".node-stage-" + [Guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $nodeArchive -DestinationPath $nodeStage -Force
            $npmCommand = Get-ChildItem $nodeStage -Recurse -Filter "npm.cmd" |
                Select-Object -First 1
            if (-not $npmCommand) { throw "cached Node export has no npm.cmd" }
            $stagedNodeRoot = Split-Path -Parent (Split-Path -Parent $npmCommand.FullName)
            Remove-Item -LiteralPath $nodeRoot -Recurse -Force -ErrorAction SilentlyContinue
            Copy-Item -LiteralPath $stagedNodeRoot -Destination $nodeRoot -Recurse -Force
        } finally {
            Remove-Item -LiteralPath $nodeStage -Recurse -Force -ErrorAction SilentlyContinue
        }
        $npmCommand = Get-ChildItem $nodeRoot -Recurse -Filter "npm.cmd" |
            Select-Object -First 1
        if (-not $npmCommand) { throw "installed cached Node tree has no npm.cmd" }
        $toolsBin = Join-Path $Root ".tools\bin"
        New-Item -ItemType Directory -Force -Path $toolsBin | Out-Null
        $leanCtxArchive = Get-CachedArtifact "lean-ctx-windows-x64" `
            $pins["LEAN_CTX_VERSION"] $python
        $leanCtxStage = Join-Path $Root (".lean-ctx-stage-" + [Guid]::NewGuid().ToString("N"))
        try {
            New-Item -ItemType Directory -Path $leanCtxStage | Out-Null
            & tar -xf $leanCtxArchive -C $leanCtxStage
            if ($LASTEXITCODE -ne 0) { throw "cached lean-ctx extraction failed" }
            $leanCtxSources = @(
                Get-ChildItem $leanCtxStage -Recurse -File -Filter "lean-ctx.exe"
            )
            if ($leanCtxSources.Count -ne 1) {
                throw "cached lean-ctx export must contain exactly one lean-ctx.exe"
            }
            $leanCtxBinary = Join-Path $toolsBin "lean-ctx.exe"
            $leanCtxTemporary = $leanCtxBinary + ".new"
            Copy-Item -LiteralPath $leanCtxSources[0].FullName `
                -Destination $leanCtxTemporary -Force
            Move-Item -LiteralPath $leanCtxTemporary `
                -Destination $leanCtxBinary -Force
        } finally {
            Remove-Item -LiteralPath $leanCtxStage -Recurse -Force `
                -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath (Join-Path $toolsBin "lean-ctx.exe.new") `
                -Force -ErrorAction SilentlyContinue
        }
        $leanCtxRoot = Join-Path $Root "state\lean-ctx"
        foreach ($directory in @("config", "data", "state", "cache")) {
            New-Item -ItemType Directory -Force `
                -Path (Join-Path $leanCtxRoot $directory) | Out-Null
        }
        $leanCtxConfig = Join-Path $leanCtxRoot "config\config.toml"
        $leanCtxConfigTemporary = $leanCtxConfig + ".new"
        try {
            Copy-Item -LiteralPath `
                (Join-Path $Root "engines\shared\lean-ctx-config.toml") `
                -Destination $leanCtxConfigTemporary -Force
            Move-Item -LiteralPath $leanCtxConfigTemporary `
                -Destination $leanCtxConfig -Force
        } finally {
            Remove-Item -LiteralPath $leanCtxConfigTemporary -Force `
                -ErrorAction SilentlyContinue
        }
        $leanCtxVersion = (& $leanCtxBinary --version).Trim()
        $expectedLeanCtxVersion = $pins["LEAN_CTX_VERSION"].TrimStart("v")
        if ($LASTEXITCODE -ne 0 -or
            $leanCtxVersion -notmatch ("^lean-ctx " +
                [regex]::Escape($expectedLeanCtxVersion) + " ")) {
            throw "installed lean-ctx does not match $($pins['LEAN_CTX_VERSION'])"
        }
        $uvArchive = Get-CachedArtifact "uv-windows-x64" $pins["UV_VERSION"] $python
        if ([IO.Path]::GetExtension($uvArchive) -ne ".zip") {
            throw "validated Windows uv export must be a .zip"
        }
        $uvStage = Join-Path $Root (".uv-stage-" + [Guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvStage -Force
            foreach ($name in @("uv.exe", "uvx.exe")) {
                $source = Get-ChildItem $uvStage -Recurse -Filter $name |
                    Select-Object -First 1
                if (-not $source) { throw "cached uv export has no $name" }
                Copy-Item -LiteralPath $source.FullName `
                    -Destination (Join-Path $toolsBin ($name + ".new")) -Force
                Move-Item -LiteralPath (Join-Path $toolsBin ($name + ".new")) `
                    -Destination (Join-Path $toolsBin $name) -Force
            }
        } finally {
            Remove-Item -LiteralPath $uvStage -Recurse -Force -ErrorAction SilentlyContinue
        }
        $env:PATH = $toolsBin + ";" + (Split-Path -Parent $npmCommand.FullName) + ";" + $env:PATH
        $env:UV_CACHE_DIR = Join-Path $cache "uv"
        Invoke-NativeChecked "uv sync" {
            & (Join-Path $toolsBin "uv.exe") sync --offline --frozen `
                --project (Join-Path $Root "env")
        }
        $mcpDuckdb = Get-CachedArtifact "python-mcp-duckdb" $pins["MCP_DUCKDB"] $python
        $mcpPostgres = Get-CachedArtifact "python-mcp-postgres" $pins["MCP_POSTGRES"] $python
        $hfCli = Get-CachedArtifact "hf-cli" $pins["HF_CLI_VERSION"] $python
        Invoke-NativeChecked "MCP cache warmup" {
            & (Join-Path $toolsBin "uvx.exe") --offline --from $mcpDuckdb `
                mcp-server-duckdb --help | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "DuckDB MCP cache warmup failed" }
            & (Join-Path $toolsBin "uvx.exe") --offline --from $mcpPostgres `
                postgres-mcp --help | Out-Null
        }
        $env:UV_TOOL_DIR = Join-Path $Root ".tools\uv-tools"
        $env:UV_TOOL_BIN_DIR = $toolsBin
        Invoke-NativeChecked "HF CLI install" {
            & (Join-Path $toolsBin "uv.exe") tool install --offline $hfCli
        }
        Write-Host "==> engines: claude-code@$($pins['CLAUDE_CODE_NPM_VERSION']) + opencode@$($pins['OPENCODE_NPM_VERSION']) + kilo@$($pins['KILO_CLI_NPM_VERSION'])"
        $claudeArchive = Get-CachedArtifact "npm-claude-code" $pins["CLAUDE_CODE_NPM_VERSION"] $python
        $opencodeArchive = Get-CachedArtifact "npm-opencode" $pins["OPENCODE_NPM_VERSION"] $python
        $kiloArchive = Get-CachedArtifact "npm-kilo-cli" $pins["KILO_CLI_NPM_VERSION"] $python
        Invoke-NativeChecked "npm" {
            & $npmCommand.FullName install -g $claudeArchive $opencodeArchive $kiloArchive
        }
        Invoke-NativeChecked "sync-skills" {
            & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\sync-skills.ps1")
        }
        Invoke-NativeChecked "skill-packs" {
            & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "harness\skill-packs\install-skill-packs.ps1")
        }
        Invoke-NativeChecked "serving setup" {
            & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") setup
        }
        foreach ($ownedTree in @(
            (Join-Path $Root ".tools\bin"),
            (Join-Path $Root ".tools\npm"),
            (Join-Path $Root ".tools\node"),
            (Join-Path $Root ".tools\win"),
            (Join-Path $Root "env\.venv"),
            (Join-Path $Root "harness\skill-packs\vendor")
        )) {
            if (-not (Test-Path -LiteralPath $ownedTree -PathType Container)) {
                throw "expected owned tree is missing: $ownedTree"
            }
            Invoke-NativeChecked "state own-tree" {
                & $python $lifecycle state own-tree --root $Root `
                    --home $env:USERPROFILE --path $ownedTree | Out-Null
            }
        }
        Invoke-NativeChecked "state mark-phase" {
            & $python $lifecycle state mark-phase --root $Root `
                --home $env:USERPROFILE --phase "windows-setup" | Out-Null
        }
        Write-Host "==> setup complete: 'oracle.ps1 serve' then 'oracle.ps1 claude'"
    }
    "harden" {
        $sub = if ($Rest.Count -ge 1 -and $Rest[0] -eq "off") { "off" } else { "on" }
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\harden-egress.ps1") $sub
    }
    "egress" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\harden-egress.ps1") $(if ($Rest.Count -ge 1) { $Rest[0] } else { "status" }) }
    "verify-egress" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\verify-egress.ps1") }
    "audit"  { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\security-audit.ps1") @Rest }
    "serve"  { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") start }
    "stop"   { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") stop }
    "restart" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") restart }
    "status" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") status }
    "service" {
        $ServiceArgs = if ($Rest.Count -gt 0) { $Rest } else { @("status") }
        & powershell -ExecutionPolicy Bypass -File `
            (Join-Path $Root "serving\serve-windows.ps1") @ServiceArgs
        exit $LASTEXITCODE
    }
    "capabilities" {
        & powershell -ExecutionPolicy Bypass -File `
            (Join-Path $Root "serving\serve-windows.ps1") capabilities @Rest
        exit $LASTEXITCODE
    }
    "verify" {
        & powershell -ExecutionPolicy Bypass -File `
            (Join-Path $Root "serving\serve-windows.ps1") verify @Rest
        exit $LASTEXITCODE
    }
    "doctor" {
        & powershell -ExecutionPolicy Bypass -File `
            (Join-Path $Root "bootstrap\doctor.ps1") @Rest
        exit $LASTEXITCODE
    }
    "claude"   { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\claude-code\launch.ps1") @Rest }
    "opencode" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\opencode\launch.ps1") @Rest }
    "kilo"     { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\kilo\launch.ps1") @Rest }
    { $_ -in @("context", "ctx") } {
        $contextArgs = if ($Rest.Count -gt 0) { $Rest } else { @("status") }
        $contextTail = if ($contextArgs.Count -gt 1) {
            @($contextArgs[1..($contextArgs.Count - 1)])
        } else {
            @()
        }
        $allowedContext = $false
        switch ($contextArgs[0]) {
            { $_ -in @("status", "doctor", "version", "help", "--version", "--help") } {
                $allowedContext = $contextTail.Count -eq 0
            }
            "gain" {
                $allowedContext = $contextTail.Count -eq 0 -or
                    ($contextTail.Count -eq 1 -and $contextTail[0] -eq "--json")
            }
            "benchmark" {
                $allowedContext = $contextTail.Count -eq 0 -or
                    ($contextTail.Count -eq 2 -and
                        $contextTail[0] -eq "run" -and $contextTail[1] -eq ".")
            }
        }
        if (-not $allowedContext) {
            [Console]::Error.WriteLine(
                "ERROR: oracle ctx exposes read-only local diagnostics only"
            )
            exit 2
        }
        $leanCtx = Join-Path $Root ".tools\bin\lean-ctx.exe"
        if (-not (Test-Path -LiteralPath $leanCtx)) {
            Write-Error "lean-ctx is missing; run bin\oracle.ps1 setup"
            exit 1
        }
        & $leanCtx @contextArgs
        exit $LASTEXITCODE
    }
    "notes" {
        # Obsidian over the repo: the operator's lens on memory/doctrine/reports
        $exe = "$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe"
        if (-not (Test-Path $exe)) {
            throw "Obsidian is optional and must be provisioned separately"
        }
        if (Test-Path $exe) { Start-Process $exe "obsidian://open?path=$([uri]::EscapeDataString($Root))" }
        else { Write-Host "Obsidian installed - launch it once from the Start menu, then open this folder as a vault: $Root" }
    }
    "agents-ui" {
        # Agent-MCP orchestration viewer (optional): watch agents/tasks/context live
        $sub = if ($Rest.Count -ge 1) { $Rest[0] } else { "start" }
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "harness\agent-mcp\setup-agent-mcp.ps1") $sub
        if ($sub -eq "start") { Start-Sleep 3; Start-Process "http://127.0.0.1:3847" }
    }
    "loops" {
        # loop-engineering CLIs (pinned in .tools\npm): audit|init|cost|sync
        $env:PATH = (Join-Path $Root ".tools\npm") + ";" + $env:PATH
        $sub = if ($Rest.Count -ge 1) { $Rest[0] } else { "audit" }
        $extra = if ($Rest.Count -gt 1) { $Rest[1..($Rest.Count - 1)] } else { @() }
        switch ($sub) {
            "audit" { & loop-audit $Root @extra }
            "init"  { & loop-init $Root @extra }
            "cost"  { & loop-cost @extra }
            "sync"  { & loop-sync $Root @extra }
            default { Write-Host "usage: oracle.ps1 loops {audit|init|cost|sync} [args]" }
        }
    }
    "mission" {
        # oracle.ps1 mission <mission.toml> [claude|opencode|kilo] [hours]
        if ($Rest.Count -lt 1) { Write-Host "usage: oracle.ps1 mission <mission.toml> [claude|opencode|kilo] [hours]"; exit 1 }
        $engine = if ($Rest.Count -ge 2) { $Rest[1] } else { "claude" }
        $hours = if ($Rest.Count -ge 3) { $Rest[2] } else { "24" }
        Invoke-Conductor @("run", $Rest[0], "--engine", $engine, "--hours", $hours)
    }
    "retro" { Invoke-Conductor @("retro", "--engine", $(if ($Rest.Count -ge 1) { $Rest[0] } else { "claude" })) }
    "state" { Invoke-Conductor @("status") }
    "ide" {
        $sub = "launch"; if ($Rest.Count -gt 0) { $sub = $Rest[0] }
        & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "connectors\ide\setup-ide.ps1") $sub
    }
    "menu" {
        while ($true) {
            Write-Host ""
            Write-Host "==================== SentiVue Oracle ===================="
            Write-Host "  Windows platform - $Root"
            Write-Host "========================================================="
            Write-Host "  8) IDE (VSCodium, local models)    d) doctor"
            Write-Host "  1) Claude Code session             2) OpenCode session"
            Write-Host "  3) sync repo to local vault        4) vault inventory"
            Write-Host "  5) download models                 6) commit + package + push"
            Write-Host "  7) open repo folder                s) serve models   t) status   q) quit"
            $c = Read-Host "choose"
            switch ($c) {
                "8" {
                    $ide = Join-Path $Root "connectors\ide\setup-ide.ps1"
                    if (Find-CodiumInstalled) { & powershell -ExecutionPolicy Bypass -File $ide launch }
                    else { & powershell -ExecutionPolicy Bypass -File $ide install; & powershell -ExecutionPolicy Bypass -File $ide launch }
                }
                "s" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") start }
                "t" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "serving\serve-windows.ps1") status }
                "1" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\claude-code\launch.ps1") }
                "2" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "engines\opencode\launch.ps1") }
                "3" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") sync }
                "4" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\vault.ps1") list }
                "5" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\download-models.ps1") }
                "6" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\finish-windows.ps1") }
                "7" { Start-Process explorer.exe $Root }
                "d" { & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "bootstrap\doctor.ps1") }
                "q" { return }
                default { }
            }
        }
    }
    default {
        Write-Host "oracle (Windows - full platform)"
        Write-Host "  setup                                     engines (pinned) + serving toolchain (one time)"
        Write-Host "  serve | stop | restart | status           admitted local serving on loopback"
        Write-Host "  service install|uninstall|...             durable per-user Scheduled Task"
        Write-Host "  capabilities | verify | doctor            truthful evidence and production probes"
        Write-Host "  harden [off] | egress [status|plan]       default-deny egress for all appliance processes"
        Write-Host "  verify-egress | audit [-Deep]             prove no leaks | full security sweep"
        Write-Host "  claude | opencode | kilo                  engine sessions on local models"
        Write-Host "  ctx [status|gain|doctor|benchmark ...]    local context runtime"
        Write-Host "  mission <toml> [engine] [hours]           self-governing mission (conductor loop)"
        Write-Host "  retro | state                             process retrospective | mission state"
        Write-Host "  agents-ui [install|start|stop|status]     orchestration viewer (Agent-MCP, optional)"
        Write-Host "  loops   audit|init|cost|sync              loop-engineering toolkit"
        Write-Host "  notes                                     Obsidian over the repo (memory lens)"
        Write-Host "  ide  [install|sync]                       VSCodium with local-only engine extensions"
        Write-Host "  menu                                      interactive operator menu"
        Write-Host "  vault   init|sync|new|clone|list|backup   local private git remote"
        Write-Host "  models  [-Dest path] [-Only name]         download models (profile-aware)"
        Write-Host "  uninstall [-Apply] [-Purge -ConfirmPurge] ownership-scoped removal"
        Write-Host "  finish  [-SkipPush]                       commit + package + push + vault sync"
    }
}

