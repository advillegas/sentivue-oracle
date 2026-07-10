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
# pins (see VERSIONS.lock)
$LlamaTag = "b9948"
$SwapVer = "236"

function Get-ActiveModels {
    # A model is active if the profile selects it OR it is already downloaded -
    # anything sitting in models\ was put there on purpose and should be served.
    # InProfile drives placement: profile models keep their slot (resident for
    # fast/embed); downloaded extras become on-demand swap models so they never
    # bloat resident memory.
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
        } | Where-Object {
            $downloaded = [bool](Get-ChildItem (Join-Path $Root "models\$($_.Name)") -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue | Select-Object -First 1)
            $_.InProfile -or $downloaded
        }
    return $rows
}

switch ($Cmd) {
    "setup" {
        New-Item -ItemType Directory -Force -Path $Tools | Out-Null
        $swapExe = Join-Path $Tools "llama-swap.exe"
        if (-not (Test-Path $swapExe)) {
            Write-Host "==> llama-swap v$SwapVer (windows_amd64)"
            $z = Join-Path $env:TEMP "llama-swap.zip"
            Invoke-WebRequest -Uri "https://github.com/mostlygeek/llama-swap/releases/download/v$SwapVer/llama-swap_${SwapVer}_windows_amd64.zip" -OutFile $z
            Expand-Archive -Path $z -DestinationPath $Tools -Force; Remove-Item $z
        }
        $serverExe = Join-Path $Tools "llama\llama-server.exe"
        if (-not (Test-Path $serverExe)) {
            Write-Host "==> llama.cpp $LlamaTag (win-vulkan-x64: AMD/Intel/NVIDIA GPU accel)"
            $z = Join-Path $env:TEMP "llamacpp.zip"
            Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaTag/llama-$LlamaTag-bin-win-vulkan-x64.zip" -OutFile $z
            Expand-Archive -Path $z -DestinationPath (Join-Path $Tools "llama") -Force; Remove-Item $z
        }
        Write-Host "serving toolchain ready under .tools\win"
    }
    "render" {
        $server = (Join-Path $Tools "llama\llama-server.exe") -replace "\\", "/"
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
            $dir = Join-Path $Root "models\$($r.Name)"
            $all = Get-ChildItem $dir -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue | Sort-Object Name
            $gguf = $all | Select-Object -First 1
            if (-not $gguf) { $missing += $r.Name; continue }
            $mp = $gguf.FullName -replace "\\", "/"
            $sizeGB = ($all | Measure-Object Length -Sum).Sum / 1GB
            $gpu = ""
            if ($sizeGB -gt [Math]::Max(1.0, $vramGB * 0.8)) { $gpu = " --n-gpu-layers 0" }
            # Context floor matters more than parallelism: agentic engines need
            # >25k tokens just to open a session, and llama-server SPLITS ctx
            # across --parallel slots. On low-RAM boxes serve one 32k slot
            # instead of two 8k slots that no engine can use.
            $ctx = [int]$r.Ctx
            $par = 2
            $kv = ""
            if ($ramGB -lt 48 -and $r.Slot -ne "embed") {
                $ctx = [Math]::Min($ctx, 32768)
                $par = 1
                # q8 KV cache halves context memory; requires flash attention
                $kv = " -fa on --cache-type-k q8_0 --cache-type-v q8_0"
            }
            $common = "--host 127.0.0.1 --port `${PORT} --jinja$gpu$kv"
            $ttl = 0
            switch ($r.Slot) {
                "embed" { $cmdline = "$server $common -m $mp --embeddings --pooling last --ctx-size $ctx" }
                "fast"  { $cmdline = "$server $common -m $mp --ctx-size $ctx --parallel $par $($r.Flags)" }
                default { $cmdline = "$server $common -m $mp --ctx-size $ctx --parallel 1 $($r.Flags)" }
            }
            if ($r.InProfile -and $r.Slot -ne "big") { $resident += $r.Name }
            else { $big += $r.Name; $ttl = 600 }   # extras: on-demand, evicted after idle
            $lines += "  `"$($r.Name)`":"
            $lines += "    cmd: $cmdline"
            $lines += "    ttl: $ttl"
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
        Set-Content -Path $Rendered -Value ($lines -join "`n")
        Write-Host "rendered $Rendered ($($big.Count) big, $($resident.Count) resident)"
    }
    "start" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath render
        if ($LASTEXITCODE -ne 0) { exit 1 }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath stop 2>$null | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $Root "state"), (Join-Path $Root "logs") | Out-Null
        $p = Start-Process -FilePath (Join-Path $Tools "llama-swap.exe") `
            -ArgumentList "--config", $Rendered, "--listen", "127.0.0.1:9099" -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $Root "logs\llama-swap.win.out.log") `
            -RedirectStandardError (Join-Path $Root "logs\llama-swap.win.err.log")
        Set-Content -Path $PidFile -Value $p.Id
        Write-Host "llama-swap starting on http://127.0.0.1:9099 (pid $($p.Id))"
    }
    "stop" {
        if (Test-Path $PidFile) {
            $procId = Get-Content $PidFile
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Remove-Item $PidFile -ErrorAction SilentlyContinue
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
