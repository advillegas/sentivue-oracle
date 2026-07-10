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
    $profileFile = Join-Path $Root "serving\models.profile"
    $active = $null
    if (Test-Path $profileFile) {
        $active = Get-Content $profileFile | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object { $_.Trim() }
    }
    $rows = Get-Content (Join-Path $Root "serving\models.manifest") |
        Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
            $f = $_ -split "\|"
            [pscustomobject]@{ Name = $f[0].Trim(); Slot = $f[3].Trim(); Ctx = $f[4].Trim(); Flags = $f[5].Trim() }
        } | Where-Object { $null -eq $active -or $active -contains $_.Name }
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
        $lines = @("listen: `"127.0.0.1:9099`"", "healthCheckTimeout: 900", "logLevel: info", "", "models:")
        $resident = @(); $big = @(); $missing = @()
        foreach ($r in Get-ActiveModels) {
            $dir = Join-Path $Root "models\$($r.Name)"
            $gguf = Get-ChildItem $dir -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue |
                Sort-Object Name | Select-Object -First 1
            if (-not $gguf) { $missing += $r.Name; continue }
            $mp = $gguf.FullName -replace "\\", "/"
            $common = "--host 127.0.0.1 --port `${PORT} --n-gpu-layers 999 --jinja"
            switch ($r.Slot) {
                "embed" { $cmdline = "$server $common -m $mp --embeddings --pooling last --ctx-size $($r.Ctx)"; $resident += $r.Name }
                "fast"  { $cmdline = "$server $common -m $mp --ctx-size $($r.Ctx) --parallel 2 $($r.Flags)"; $resident += $r.Name }
                default { $cmdline = "$server $common -m $mp --ctx-size $($r.Ctx) --parallel 1 $($r.Flags)"; $big += $r.Name }
            }
            $lines += "  `"$($r.Name)`":"
            $lines += "    cmd: $cmdline"
            $lines += "    ttl: 0"
        }
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
            -ArgumentList "--config", $Rendered -WindowStyle Hidden -PassThru `
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
