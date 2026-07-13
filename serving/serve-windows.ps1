# serve-windows.ps1 - Windows service twin for policy-bound local serving.
#
# Resource/profile/admission/render/probe behavior lives in verification/serving.py.
# This wrapper owns only the Windows cached tool install and Scheduled Task lifecycle.
# NVIDIA capacity comes only from nvidia-smi exact MiB evidence in the shared core;
# Win32_VideoController memory is intentionally never consulted.
param(
    [Parameter(Position = 0)][string]$Cmd = "status",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root ".tools\win"
$NativeDir = Join-Path $Tools "llama"
$Server = Join-Path $NativeDir "llama-server.exe"
$Swap = Join-Path $Tools "llama-swap.exe"
$Generated = Join-Path $Root "state\generated\serving"
$Config = Join-Path $Generated "llama-swap.yaml"
$Admission = Join-Path $Generated "admission.json"
$PidRecord = Join-Path $Generated "service.pid.json"
$TaskName = "SentiVueOracleServing"
$DependencyCache = if ($env:ORACLE_DEPENDENCY_CACHE) {
    $env:ORACLE_DEPENDENCY_CACHE
} else {
    Join-Path $Root "incoming\dependency-cache"
}
$ArtifactManifest = Join-Path $DependencyCache "manifest.json"

function Find-Python {
    $Candidates = @(
        (Join-Path $Root "env\.venv\Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )
    $Command = Get-Command python -ErrorAction SilentlyContinue
    if ($Command -and $Command.Source -notmatch "WindowsApps") {
        $Candidates += $Command.Source
    }
    foreach ($Candidate in $Candidates) {
        if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            continue
        }
        & $Candidate -c "import sys; raise SystemExit(sys.version_info < (3, 12))" *> $null
        if ($LASTEXITCODE -eq 0) { return $Candidate }
    }
    throw "Python 3.12 or newer is required for shared serving validation"
}

$Python = Find-Python
$Serving = Join-Path $Root "verification\serving.py"
$Lifecycle = Join-Path $Root "verification\lifecycle.py"

function Invoke-Serving {
    param([string[]]$Arguments)
    & $Python $Serving @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "shared serving command failed: $($Arguments -join ' ')"
    }
}

function Get-LockedVersion {
    param([string]$Name)
    $Line = Get-Content (Join-Path $Root "VERSIONS.lock") |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -First 1
    if (-not $Line) { throw "missing $Name in VERSIONS.lock" }
    return ((($Line -split "=", 2)[1]) -split "#", 2)[0].Trim()
}

function Get-CachedArtifact {
    param([string]$Id, [string]$Version)
    $Arguments = @(
        $Lifecycle, "artifact-path",
        "--manifest", $ArtifactManifest,
        "--cache", $DependencyCache,
        "--artifact-id", $Id,
        "--expected-version", $Version,
        "--expected-requested-version", $Version,
        "--root", $Root,
        "--reproducible"
    )
    $Path = (& $Python @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "cached artifact validation failed: $Id"
    }
    return $Path.Trim()
}

function Install-ServingTools {
    $SwapVersion = Get-LockedVersion "LLAMA_SWAP_VERSION"
    $LlamaVersion = Get-LockedVersion "LLAMA_CPP_WIN_TAG"
    New-Item -ItemType Directory -Force -Path $Tools | Out-Null

    $SwapArchive = Get-CachedArtifact "llama-swap-windows-amd64" $SwapVersion
    $SwapStage = Join-Path $Tools (".swap-stage-" + [Guid]::NewGuid().ToString("N"))
    try {
        Expand-Archive -LiteralPath $SwapArchive -DestinationPath $SwapStage -Force
        $Candidate = Get-ChildItem $SwapStage -Recurse -Filter "llama-swap.exe" |
            Select-Object -First 1
        if (-not $Candidate) { throw "llama-swap archive has no executable" }
        Copy-Item -LiteralPath $Candidate.FullName -Destination ($Swap + ".new") -Force
        Move-Item -LiteralPath ($Swap + ".new") -Destination $Swap -Force
    } finally {
        Remove-Item -LiteralPath $SwapStage -Recurse -Force -ErrorAction SilentlyContinue
    }

    $ServerArchive = Get-CachedArtifact "llama-cpp-windows-vulkan" $LlamaVersion
    $ServerStage = Join-Path $Tools (".server-stage-" + [Guid]::NewGuid().ToString("N"))
    $NewServer = Join-Path $Tools "llama.new"
    try {
        Expand-Archive -LiteralPath $ServerArchive -DestinationPath $ServerStage -Force
        $Candidate = Get-ChildItem $ServerStage -Recurse -Filter "llama-server.exe" |
            Select-Object -First 1
        if (-not $Candidate) { throw "llama.cpp archive has no llama-server.exe" }
        Remove-Item -LiteralPath $NewServer -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath (Split-Path -Parent $Candidate.FullName) `
            -Destination $NewServer -Recurse -Force
        Remove-Item -LiteralPath (Join-Path $Tools "llama") `
            -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $NewServer -Destination (Join-Path $Tools "llama")
    } finally {
        Remove-Item -LiteralPath $ServerStage -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "serving toolchain ready (Vulkan/CPU scope)"
}

function Render-Serving {
    if (-not (Test-Path -LiteralPath $Server -PathType Leaf)) {
        throw "llama-server is missing; run serving\serve-windows.ps1 setup"
    }
    if (-not (Test-Path -LiteralPath $Swap -PathType Leaf)) {
        throw "llama-swap is missing; run serving\serve-windows.ps1 setup"
    }
    $SwapArchive = Get-CachedArtifact "llama-swap-windows-amd64" `
        (Get-LockedVersion "LLAMA_SWAP_VERSION")
    $ServerArchive = Get-CachedArtifact "llama-cpp-windows-vulkan" `
        (Get-LockedVersion "LLAMA_CPP_WIN_TAG")
    Invoke-Serving @(
        "verify-binary", "--installed", $Swap,
        "--archive", $SwapArchive, "--member", "llama-swap.exe"
    )
    Invoke-Serving @(
        "verify-binary-tree", "--installed-directory", $NativeDir,
        "--archive", $ServerArchive, "--anchor-member", "llama-server.exe"
    )
    $Backend = if ($env:ORACLE_BACKEND) { $env:ORACLE_BACKEND } else { "cpu" }
    if ($Backend -notin @("vulkan", "cpu")) {
        throw "the policy-bound Windows toolchain supports explicit vulkan or cpu only"
    }
    $PreviousVulkan = $env:ORACLE_VULKAN_AVAILABLE
    $env:ORACLE_VULKAN_AVAILABLE = if ($Backend -eq "vulkan") { "1" } else { "0" }
    try {
        Invoke-Serving @(
            "render", "--root", $Root, "--server", $Server,
            "--llama-swap", $Swap,
            "--platform", "windows", "--backend", $Backend
        )
    } finally {
        $env:ORACLE_VULKAN_AVAILABLE = $PreviousVulkan
    }
}

function Sync-EngineConfigs {
    & (Join-Path $Root "connectors\ide\sync-models.ps1") -Root $Root
    if ($LASTEXITCODE -ne 0) {
        throw "engine model configuration synchronization failed"
    }
}

function Get-ServiceArguments {
    $Raw = @(
        $Serving, "service-run",
        "--root", $Root,
        "--llama-swap", $Swap,
        "--config", $Config,
        "--admission", $Admission,
        "--gateway-host", "127.0.0.1",
        "--gateway-port", "9099",
        "--upstream-host", "127.0.0.1",
        "--upstream-port", "9098"
    )
    $Quoted = @()
    foreach ($Value in $Raw) {
        $Part = (& $Python $Lifecycle quote-argument --platform windows --value $Value)
        if ($LASTEXITCODE -ne 0) { throw "could not quote Scheduled Task arguments" }
        $Quoted += $Part.Trim()
    }
    return ($Quoted -join " ")
}

function Test-OwnedScheduledTask {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $Task) { return $null }
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) {
        throw "refusing Scheduled Task operation: unexpected action count"
    }
    $Action = $Actions[0]
    $ExpectedPython = [IO.Path]::GetFullPath($Python)
    $ObservedPython = [IO.Path]::GetFullPath([string]$Action.Execute)
    if (-not $ObservedPython.Equals(
            $ExpectedPython, [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Action.Arguments -ne (Get-ServiceArguments) -or
        [IO.Path]::GetFullPath([string]$Action.WorkingDirectory) -ne
            [IO.Path]::GetFullPath($Root)) {
        throw "refusing Scheduled Task operation: task identity is not Oracle-owned"
    }
    return $Task
}

function Install-Service {
    Render-Serving
    Sync-EngineConfigs
    if (-not (Test-Path -LiteralPath $Swap -PathType Leaf)) {
        throw "llama-swap is missing; run serving\serve-windows.ps1 setup"
    }
    $Existing = Test-OwnedScheduledTask
    $Existing | Out-Null
    $Action = New-ScheduledTaskAction -Execute $Python `
        -Argument (Get-ServiceArguments) -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Principal $Principal -Settings $Settings -Force | Out-Null
    & $Python $Lifecycle state own-service --root $Root `
        --home $env:USERPROFILE --service-kind "windows-scheduled-task" `
        --identifier $TaskName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false `
            -ErrorAction SilentlyContinue
        throw "Scheduled Task ownership registration failed"
    }
    Write-Host "installed per-user Scheduled Task: $TaskName"
}

function Test-OwnedServiceProcess {
    if (-not (Test-Path -LiteralPath $PidRecord -PathType Leaf)) { return $null }
    try {
        $Record = Get-Content -Raw -LiteralPath $PidRecord | ConvertFrom-Json
        if ($Record.schema_version -ne 1 -or $Record.pid -notmatch "^[0-9]+$") {
            throw "invalid service PID record"
        }
        $Process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
        if (-not $Process) { return $null }
        $ExpectedPython = [IO.Path]::GetFullPath($Python)
        if (-not $Process.Path -or
            -not $Process.Path.Equals($ExpectedPython, [StringComparison]::OrdinalIgnoreCase)) {
            throw "PID $($Record.pid) is not the Oracle serving Python"
        }
        $Cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.pid)"
        if (-not $Cim -or $Cim.CommandLine -notmatch "verification[\\/]serving\.py" -or
            $Cim.CommandLine -notmatch "service-run") {
            throw "PID $($Record.pid) command line is not the Oracle service"
        }
        $Observed = [DateTimeOffset]($Process.StartTime.ToUniversalTime())
        if ([Math]::Abs($Observed.ToUnixTimeSeconds() - [double]$Record.started_at) -gt 2) {
            throw "PID $($Record.pid) start time does not match the service record"
        }
        return $Process
    } catch {
        throw "refusing service process operation: $($_.Exception.Message)"
    }
}

function Stop-ServiceProcess {
    $Task = Test-OwnedScheduledTask
    if ($Task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 750
    $Process = Test-OwnedServiceProcess
    if ($Process) {
        & taskkill.exe /F /T /PID $Process.Id | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "failed to stop the owned serving process tree" }
    }
    Write-Host "serving stopped"
}

function Show-Status {
    $Task = Test-OwnedScheduledTask
    if ($Task) { Write-Host "service: INSTALLED ($($Task.State))" }
    else { Write-Host "service: NOT INSTALLED" }
    $Process = Test-OwnedServiceProcess
    if ($Process) { Write-Host "process: OWNED pid=$($Process.Id)" }
    else { Write-Host "process: DOWN" }
    try {
        $Response = Invoke-RestMethod -Uri "http://127.0.0.1:9099/health" -TimeoutSec 3
        $Response | Out-Null
        Write-Host "gateway: HEALTHY (127.0.0.1:9099)"
    } catch {
        Write-Host "gateway: DOWN"
    }
}

switch ($Cmd) {
    "setup" { Install-ServingTools }
    "render" { Render-Serving }
    "install" { Install-Service }
    "uninstall" {
        Stop-ServiceProcess
        if (Test-OwnedScheduledTask) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Write-Host "serving Scheduled Task removed"
    }
    "start" {
        if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
            Install-Service
        } else {
            Test-OwnedScheduledTask | Out-Null
            Render-Serving
            Sync-EngineConfigs
        }
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "serving starting through $TaskName on 127.0.0.1:9099"
    }
    "stop" { Stop-ServiceProcess }
    "restart" {
        Stop-ServiceProcess
        Test-OwnedScheduledTask | Out-Null
        Render-Serving
        Sync-EngineConfigs
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "serving restarted"
    }
    "status" { Show-Status }
    "capabilities" {
        $CapabilityArgs = @("capabilities", "--root", $Root)
        $HasBackend = @(
            $Rest | Where-Object {
                $_ -eq "--backend" -or $_ -like "--backend=*"
            }
        ).Count -gt 0
        if (-not $HasBackend) {
            $Backend = if ($env:ORACLE_BACKEND) {
                $env:ORACLE_BACKEND
            } else {
                "cpu"
            }
            $CapabilityArgs += @("--backend", $Backend)
        }
        & $Python $Serving @CapabilityArgs @Rest
        exit $LASTEXITCODE
    }
    "verify" {
        & $Python $Serving verify --root $Root @Rest
        exit $LASTEXITCODE
    }
    default {
        Write-Host "usage: serve-windows.ps1 {setup|render|install|uninstall|start|stop|restart|status|capabilities|verify}"
        exit 2
    }
}
