# serve-windows.ps1 - full model serving on the Windows node.
# Same architecture as the Mac: llama-swap front door (Anthropic + OpenAI wire)
# hot-swapping llama-server models per serving\models.manifest + models.profile.
#
#   powershell -File serving\serve-windows.ps1 setup     fetch pinned llama.cpp (Vulkan) + llama-swap
#   powershell -File serving\serve-windows.ps1 start     render config + launch (background)
#   powershell -File serving\serve-windows.ps1 status|stop
param([Parameter(Position = 0)][string]$Cmd = "status")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root ".tools\win"
$Rendered = Join-Path $Root "serving\llama-swap.rendered.win.yaml"
$PidFile = Join-Path $Root "state\llama-swap.pid"
$DependencyCache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
$ArtifactManifest = Join-Path $DependencyCache "manifest.json"

function Get-LockedVersion([string]$Name) {
    $line = Get-Content (Join-Path $Root "VERSIONS.lock") |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -First 1
    if (-not $line) { throw "missing $Name in VERSIONS.lock" }
    return ((($line -split "=", 2)[1]) -split "#", 2)[0].Trim()
}

function Get-CachedArtifact([string]$Id, [string]$Version) {
    $python = Join-Path $Root "env\.venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    $lifecycleArgs = @(
        (Join-Path $Root "verification\lifecycle.py"), "artifact-path",
        "--manifest", $ArtifactManifest, "--cache", $DependencyCache,
        "--artifact-id", $Id, "--expected-version", $Version,
        "--expected-requested-version", $Version,
        "--root", $Root, "--reproducible"
    )
    $path = (& $python @lifecycleArgs)
    if ($LASTEXITCODE -ne 0) { throw "cached artifact validation failed: $Id" }
    return $path.Trim()
}

function Write-Utf8NoBomAtomic([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = Join-Path $parent (".{0}.{1}.tmp" -f
        [IO.Path]::GetFileName($Path), [Guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllText(
            $temporary, $Text, (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Quote-CommandArgument([string]$Value) {
    $python = Join-Path $Root "env\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    $quoted = & $python (Join-Path $Root "verification\lifecycle.py") quote-argument --platform windows --value $Value
    if ($LASTEXITCODE -ne 0) { throw "safe command quoting failed" }
    return $quoted.Trim()
}

function Get-ValidatedModelPath([string]$ModelName) {
    $python = Join-Path $Root "env\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    $modelPath = & $python (Join-Path $Root "verification\lifecycle.py") `
        model-path --model-name $ModelName --root $Root --cache $DependencyCache
    if ($LASTEXITCODE -ne 0) {
        throw "policy-bound model snapshot is unavailable: $ModelName"
    }
    return $modelPath.Trim()
}

$LlamaTag = Get-LockedVersion "LLAMA_CPP_WIN_TAG"
$SwapVer = Get-LockedVersion "LLAMA_SWAP_VERSION"

function Get-ActiveModels {
    # No profile means all declared models are selected. Local files never add
    # themselves to the serving set; model-path validates tracked authority.
    $profileFile = Join-Path $Root "serving\models.profile"
    $active = $null
    if (Test-Path $profileFile) {
        $active = Get-Content $profileFile | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object { $_.Trim() }
    }
    $rows = Get-Content (Join-Path $Root "serving\models.manifest") |
        Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
            $f = $_ -split "\|"
            [pscustomobject]@{ Name = $f[0].Trim(); Slot = $f[3].Trim(); Ctx = $f[4].Trim(); Flags = $f[5].Trim()
                               InProfile = ($null -eq $active) -or ($active -contains $f[0].Trim()) }
        } | Where-Object { $_.InProfile }
    return $rows
}

switch ($Cmd) {
    "setup" {
        New-Item -ItemType Directory -Force -Path $Tools | Out-Null
        $swapExe = Join-Path $Tools "llama-swap.exe"
        Write-Host "==> llama-swap v$SwapVer (windows_amd64)"
        $z = Get-CachedArtifact "llama-swap-windows-amd64" $SwapVer
        $swapStage = Join-Path $Tools (".swap-stage-" + [Guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $z -DestinationPath $swapStage -Force
            $stagedSwap = Get-ChildItem $swapStage -Recurse -Filter "llama-swap.exe" |
                Select-Object -First 1
            if (-not $stagedSwap) { throw "llama-swap archive has no llama-swap.exe" }
            Copy-Item -LiteralPath $stagedSwap.FullName -Destination ($swapExe + ".new") -Force
            Move-Item -LiteralPath ($swapExe + ".new") -Destination $swapExe -Force
        } finally {
            Remove-Item -LiteralPath $swapStage -Recurse -Force -ErrorAction SilentlyContinue
        }
        $serverExe = Join-Path $Tools "llama\llama-server.exe"
        Write-Host "==> llama.cpp $LlamaTag (win-vulkan-x64: AMD/Intel/NVIDIA GPU accel)"
        $z = Get-CachedArtifact "llama-cpp-windows-vulkan" $LlamaTag
        $serverStage = Join-Path $Tools (".server-stage-" + [Guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $z -DestinationPath $serverStage -Force
            $stagedServer = Get-ChildItem $serverStage -Recurse -Filter "llama-server.exe" |
                Select-Object -First 1
            if (-not $stagedServer) { throw "llama.cpp archive has no llama-server.exe" }
            $stagedRoot = Split-Path -Parent $stagedServer.FullName
            $newServer = Join-Path $Tools "llama.new"
            Remove-Item -LiteralPath $newServer -Recurse -Force -ErrorAction SilentlyContinue
            Copy-Item -LiteralPath $stagedRoot -Destination $newServer -Recurse -Force
            Remove-Item -LiteralPath (Join-Path $Tools "llama") -Recurse -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $newServer -Destination (Join-Path $Tools "llama")
        } finally {
            Remove-Item -LiteralPath $serverStage -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "serving toolchain ready under .tools\win"
    }
    "render" {
        $server = Join-Path $Tools "llama\llama-server.exe"
        $serverArg = Quote-CommandArgument $server
        # Hardware-adaptive placement: models larger than the GPU's dedicated
        # VRAM run pure-CPU (--n-gpu-layers 0) - on iGPU boxes Vulkan otherwise
        # dies allocating weights or KV cache (ErrorOutOfDeviceMemory). Smaller
        # models let llama.cpp auto-fit layers. Low-RAM boxes also cap context.
        $vramGB = 0
        try {
            $vramGB = (Get-CimInstance Win32_VideoController |
                Where-Object { $_.Name -notmatch "Virtual|Remote|Basic" } |
                ForEach-Object { $_.AdapterRAM / 1GB } | Measure-Object -Maximum).Maximum
        } catch {}
        $ramGB = [Math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
        # NOTE: the listen address is a CLI flag (--listen), not a config key.
        $lines = @("healthCheckTimeout: 900", "logLevel: info", "", "models:")
        $resident = @(); $big = @(); $missing = @()
        foreach ($r in Get-ActiveModels) {
            try {
                $mp = Get-ValidatedModelPath $r.Name
            } catch {
                $missing += $r.Name
                continue
            }
            $modelArg = Quote-CommandArgument $mp
            $sizeGB = (Get-Item -LiteralPath $mp).Length / 1GB
            $gpu = ""
            if ($sizeGB -gt [Math]::Max(1.0, $vramGB * 0.8)) { $gpu = " --n-gpu-layers 0" }
            # Context floor matters more than parallelism: agentic engines need
            # >25k tokens to OPEN a session and blow past 50k mid-task (observed
            # 53k, seed brain V14/E19 incident). llama-server SPLITS ctx across
            # --parallel slots, so low-RAM boxes serve ONE 64k slot with q8 KV
            # (~3 GB for a 30B GQA model) instead of parallel slots too small
            # for any engine to use.
            $ctx = [int]$r.Ctx
            $par = 2
            $kv = ""
            if ($ramGB -lt 48 -and $r.Slot -ne "embed") {
                $ctx = [Math]::Min($ctx, 65536)
                $par = 1
                # q8 KV cache halves context memory; requires flash attention
                $kv = " -fa on --cache-type-k q8_0 --cache-type-v q8_0"
            }
            # --cache-reuse: chunked KV prefix reuse so slightly-divergent prompts
            # (growing agent conversations) keep their cached prefix (measured
            # 115s cold vs 2s cached prefill on this box)
            $common = "--host 127.0.0.1 --port `${PORT} --jinja --cache-reuse 256$gpu$kv"
            $ttl = 0
            switch ($r.Slot) {
                "embed" { $cmdline = "$serverArg $common -m $modelArg --embeddings --pooling last --ctx-size $ctx" }
                "fast"  { $cmdline = "$serverArg $common -m $modelArg --ctx-size $ctx --parallel $par $($r.Flags)" }
                default { $cmdline = "$serverArg $common -m $modelArg --ctx-size $ctx --parallel 1 $($r.Flags)" }
            }
            if ($r.InProfile -and $r.Slot -ne "big") { $resident += $r.Name }
            else { $big += $r.Name; $ttl = 600 }   # extras: on-demand, evicted after idle
            $lines += "  `"$($r.Name)`":"
            $lines += "    cmd: $cmdline"
            $lines += "    ttl: $ttl"
            # OpenAI-compatible aliases so stock tools (Agent-MCP RAG, anything
            # defaulting to OpenAI model names) transparently ride local models.
            if ($r.Slot -eq "embed" -and -not $script:embedAliased) {
                $script:embedAliased = $true
                $lines += "    aliases:"
                $lines += "      - text-embedding-3-large"
                $lines += "      - text-embedding-3-small"
                $lines += "      - text-embedding-ada-002"
            } elseif ($r.Slot -eq "fast" -and -not $script:fastAliased) {
                $script:fastAliased = $true
                $lines += "    aliases:"
                $lines += "      - gpt-4o-mini"
                $lines += "      - gpt-4o"
            }
        }
        Write-Host ("hardware: {0} GB RAM, {1:N1} GB VRAM -> big-model placement {2}" -f `
            $ramGB, $vramGB, $(if ($vramGB -lt 8) { "CPU" } else { "GPU auto-fit" }))
        if ($missing) { Write-Host "NOTE: not downloaded yet (skipped): $($missing -join ', ')" }
        if (-not $resident) { Write-Host "ERROR: no resident (fast/embed) models downloaded - run the downloader first"; exit 1 }
        $lines += ""
        $lines += "groups:"
        if ($big) {
            $lines += "  big:"; $lines += "    swap: true"; $lines += "    exclusive: false"; $lines += "    members:"
            foreach ($m in $big) { $lines += "      - `"$m`"" }
        }
        $lines += "  resident:"; $lines += "    swap: false"; $lines += "    exclusive: false"; $lines += "    persistent: true"; $lines += "    members:"
        foreach ($m in $resident) { $lines += "      - `"$m`"" }
        Write-Utf8NoBomAtomic $Rendered (($lines -join "`n") + "`n")
        Write-Host "rendered $Rendered ($($big.Count) big, $($resident.Count) resident)"
    }
    "start" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath render
        if ($LASTEXITCODE -ne 0) { exit 1 }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath stop 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not safely stop the previous serving process" }
        New-Item -ItemType Directory -Force -Path (Join-Path $Root "state"), (Join-Path $Root "logs") | Out-Null
        $python = Join-Path $Root "env\.venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $python)) {
            $python = (Get-Command python -ErrorAction Stop).Source
        }
        $lifecycle = Join-Path $Root "verification\lifecycle.py"
        & $python $lifecycle state init --root $Root --home $env:USERPROFILE | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "install state initialization failed" }
        $p = Start-Process -FilePath (Join-Path $Tools "llama-swap.exe") `
            -ArgumentList "--config", $Rendered, "--listen", "127.0.0.1:9099" -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $Root "logs\llama-swap.win.out.log") `
            -RedirectStandardError (Join-Path $Root "logs\llama-swap.win.err.log")
        try {
            Write-Utf8NoBomAtomic $PidFile ("{0}`n" -f $p.Id)
            & $python $lifecycle state own --root $Root --home $env:USERPROFILE `
                --path $PidFile | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "PID ownership registration failed" }
            & $python $lifecycle state own-service --root $Root `
                --home $env:USERPROFILE --service-kind "windows-pid-file" `
                --identifier "state/llama-swap.pid" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "process ownership registration failed" }
        } catch {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
            throw
        }
        Write-Host "llama-swap starting on http://127.0.0.1:9099 (pid $($p.Id))"
    }
    "stop" {
        if (Test-Path $PidFile) {
            [int]$procId = (Get-Content $PidFile -Raw).Trim()
            $process = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($process) {
                $expected = [IO.Path]::GetFullPath((Join-Path $Tools "llama-swap.exe"))
                if (-not $process.Path -or
                    -not $process.Path.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "refusing to stop PID $procId because it is not Oracle llama-swap"
                }
                Stop-Process -Id $procId -Force -ErrorAction Stop
            }
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction Stop
            Write-Host "stopped"
        } else { Write-Host "not running (no pidfile)" }
    }
    "status" {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3 | Out-Null
            Write-Host "llama-swap: HEALTHY (http://127.0.0.1:9099)"
            try { (Invoke-RestMethod -Uri "http://127.0.0.1:9099/running" -TimeoutSec 3).running | ForEach-Object { Write-Host "  $($_.model) [$($_.state)]" } } catch {}
        } catch { Write-Host "llama-swap: DOWN (serve-windows.ps1 start)" }
    }
    default { Write-Host "usage: serve-windows.ps1 {setup|render|start|stop|status}" }
}
