# build-installers.ps1 - produce double-clickable, self-extracting installers.
#
#   SentiVue-Oracle-Installer-<ver>.command   macOS: Terminal wizard + embedded tar.gz
#   SentiVue-Oracle-Setup-<ver>.cmd           Windows: console wizard + embedded zip (base64)
#
# Both contain the full repo payload (git archive = tracked files only) and walk
# the user through installation with prompts - no commands required.
param(
    [string]$Version = "v0.1.0",
    [string]$OutDir = (Join-Path $env:TEMP "oracle-release")
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$tarball = Join-Path $OutDir "payload.tar.gz"
$zipball = Join-Path $OutDir "payload.zip"
git archive --format=tar.gz --prefix=sentivue-oracle/ -o $tarball $Version
git archive --format=zip    --prefix=sentivue-oracle/ -o $zipball $Version

# (The retired Rust desk app is no longer embedded; the product is the IDE.)

# ============================== macOS .command ==============================
$macLines = @(
'#!/bin/bash'
'# SentiVue Oracle installer - double-click to run. If macOS blocks it'
'# (downloaded file), right-click the file and choose Open the first time.'
'set -uo pipefail'
'trap ''code=$?; echo; echo "INSTALLER ERROR (exit $code). Nothing destructive happened."; echo "You can re-run this installer any time."; read -r -p "Press Enter to close... "'' ERR'
'set -e'
'clear'
'echo "=============================================================="'
'echo "        SentiVue Oracle - guided installation"'
'echo "  Offline agentic workstation: local models, dual engines,"'
'echo "  git vault, mission conductor, controlled-internet envoy."'
'echo "=============================================================="'
'echo'
'DEFAULT="$HOME/sentivue-oracle"'
'read -r -p "Press ENTER to install to $DEFAULT (or type another path): " DEST'
'DEST="${DEST:-$DEFAULT}"'
'if [[ -e "$DEST/README.md" ]]; then'
'  echo "==> existing installation found - files will be updated in place"'
'fi'
'mkdir -p "$DEST"'
'echo "==> unpacking..."'
'PAYLOAD_LINE=$(awk "/^__PAYLOAD_BELOW__$/ {print NR + 1; exit 0}" "$0")'
'tail -n +"$PAYLOAD_LINE" "$0" | tar -xz --strip-components=1 -C "$DEST"'
'chmod +x "$DEST/install" "$DEST/bin/"* "$DEST/bootstrap/"*.sh "$DEST/serving/service.sh" 2>/dev/null || true'
'echo "==> installed to $DEST"'
'echo'
'echo "Continuing into the guided setup (it will prompt for the model profile:"'
'echo "  full ~700 GB / coder ~315 GB / minimal ~40 GB smoke test)."'
'echo "Every phase is resumable - re-running this installer is always safe."'
'echo'
'cd "$DEST"'
'bash install || true'
'echo'
'read -r -p "Press Enter to close... "'
'exit 0'
'__PAYLOAD_BELOW__'
)
$macPath = Join-Path $OutDir "SentiVue-Oracle-Installer-$Version.command"
$macScript = ($macLines -join "`n") + "`n"
[IO.File]::WriteAllBytes($macPath, [Text.Encoding]::UTF8.GetBytes($macScript) + [IO.File]::ReadAllBytes($tarball))
Write-Host ("==> {0}  ({1:N1} MB)" -f (Split-Path -Leaf $macPath), ((Get-Item $macPath).Length / 1MB))

# ============================== Windows .cmd =================================
$psPayload = @'
$ErrorActionPreference = "Stop"
try {
    Write-Host "=============================================================="
    Write-Host "        SentiVue Oracle - Windows node setup"
    Write-Host "  Model pre-downloader + local git vault for the ecosystem."
    Write-Host "  (The full appliance installs on the Mac via the .command)"
    Write-Host "=============================================================="
    Write-Host ""
    $default = Join-Path $env:USERPROFILE "sentivue-oracle"
    $dest = Read-Host "Press ENTER to install to $default (or type another path)"
    if (-not $dest) { $dest = $default }
    if (Test-Path (Join-Path $dest "README.md")) {
        Write-Host "==> existing installation found - files will be updated in place"
    }
    Write-Host "==> unpacking..."
    $raw = [IO.File]::ReadAllText($env:ORACLE_SETUP_SELF)
    $mk = "#==" + "B64PAYLOAD" + "==#"   # built dynamically so the literal appears once in this file
    $b64 = ($raw -split $mk)[1] -replace "[^A-Za-z0-9+/=]", ""
    $tmp = Join-Path $env:TEMP "oracle-setup-payload.zip"
    [IO.File]::WriteAllBytes($tmp, [Convert]::FromBase64String($b64))
    $stage = Join-Path $env:TEMP "oracle-setup-stage"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive -Path $tmp -DestinationPath $stage -Force
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item (Join-Path $stage "sentivue-oracle\*") $dest -Recurse -Force
    Remove-Item $tmp, $stage -Recurse -Force
    Write-Host "==> installed to $dest"
    if (Get-Command git -ErrorAction SilentlyContinue) {
        try {
            if (-not (Test-Path (Join-Path $dest ".git"))) {
                git -C $dest init -b main 2>&1 | Out-Null
                git -C $dest add -A 2>&1 | Out-Null
                git -C $dest -c user.name="oracle" -c user.email="oracle@localhost" commit -q -m "installer import" 2>&1 | Out-Null
            }
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\vault.ps1") init 2>&1 | Out-Null
            Write-Host "==> local git vault: ready (private offline backup remote)"
        } catch { Write-Host "==> vault setup skipped ($($_.Exception.Message))" }
    } else {
        Write-Host "==> git not found - vault can be set up later: bin\oracle.ps1 vault init"
    }
    # The ONE executable: place the prebuilt desk app and point the shortcut at it.
    $deskBin = Join-Path $dest "desk\target\release\oracle-desk.exe"
    $prebuilt = Join-Path $dest "desk\prebuilt\oracle-desk-windows-x64.exe"
    if ((Test-Path $prebuilt) -and -not (Test-Path $deskBin)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $deskBin) | Out-Null
        Copy-Item $prebuilt $deskBin -Force
        Write-Host "==> oracle-desk.exe installed (the platform launcher)"
    }
    try {
        $ws = New-Object -ComObject WScript.Shell
        $desk = [Environment]::GetFolderPath("Desktop")
        $lnk = $ws.CreateShortcut((Join-Path $desk "SentiVue Oracle.lnk"))
        if (Test-Path $deskBin) {
            $lnk.TargetPath = $deskBin
            $lnk.Arguments = ""
            $lnk.IconLocation = "$deskBin,0"
        } else {
            $lnk.TargetPath = "powershell.exe"
            $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File ""$dest\bin\oracle.ps1"" menu"
            $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,73"
        }
        $lnk.WorkingDirectory = $dest
        $lnk.Description = "SentiVue Oracle - self-contained development ecosystem"
        $lnk.Save()
        Write-Host "==> desktop shortcut created: SentiVue Oracle (one-click platform)"
    } catch { Write-Host "==> desktop shortcut skipped ($($_.Exception.Message))" }
    # ---- hardware-adaptive model profile (any machine installs properly) ----
    $ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    $vram = 0
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        try { $vram = [math]::Round(((& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits) | Measure-Object -Sum).Sum / 1024) } catch {}
    }
    $budget = [math]::Max($ram - 8, $vram)
    $profiles = Get-Content (Join-Path $dest "serving\profiles.conf") |
        Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
            $f = $_ -split "\|"
            [pscustomobject]@{ Name = $f[0].Trim(); Min = [int]$f[1].Trim(); Models = $f[2].Trim()
                               Opus = $f[3].Trim(); Sonnet = $f[4].Trim(); Haiku = $f[5].Trim(); Dl = $f[6].Trim() }
        }
    $suggest = $profiles | Where-Object { $budget -ge $_.Min } | Select-Object -First 1
    if (-not $suggest) { $suggest = $profiles[$profiles.Count - 1] }
    Write-Host ""
    $gputxt = ""
    if ($vram -gt 0) { $gputxt = ", $vram GB VRAM" }
    Write-Host "Hardware detected: $ram GB RAM$gputxt  ->  suggested profile: $($suggest.Name) ($($suggest.Dl))"
    $names = @($profiles | ForEach-Object { $_.Name })
    $idx = [array]::IndexOf($names, $suggest.Name); if ($idx -lt 0) { $idx = 0 }
    if ([Console]::IsInputRedirected) {
        $chosen = Read-Host "profile [ENTER = $($suggest.Name)]"
        $sel = $profiles | Where-Object { $_.Name -eq $chosen } | Select-Object -First 1
        if (-not $sel) { $sel = $suggest }
    } else {
        Write-Host "Use Up/Down arrows, ENTER to confirm:"
        $top = [Console]::CursorTop
        while ($true) {
            [Console]::SetCursorPosition(0, $top)
            for ($i = 0; $i -lt $profiles.Count; $i++) {
                $p = $profiles[$i]
                $mark = "   "; if ($i -eq $idx) { $mark = " > " }
                $line = ("{0}{1,-6} needs >= {2,3} GB memory   download {3}" -f $mark, $p.Name, $p.Min, $p.Dl)
                $fg = "Gray"; if ($i -eq $idx) { $fg = "Cyan" }
                Write-Host ($line.PadRight([Console]::WindowWidth - 1)) -ForegroundColor $fg
            }
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq "UpArrow") { $idx = ($idx - 1 + $profiles.Count) % $profiles.Count }
            elseif ($k.Key -eq "DownArrow") { $idx = ($idx + 1) % $profiles.Count }
            elseif ($k.Key -eq "Enter") { break }
        }
        $sel = $profiles[$idx]
    }
    if ($sel.Name -eq "full") { Remove-Item (Join-Path $dest "serving\models.profile") -ErrorAction SilentlyContinue }
    else { Set-Content -Path (Join-Path $dest "serving\models.profile") -Value (($sel.Models -split ",") -join "`n") }
    Set-Content -Path (Join-Path $dest "serving\tiers.env") -Value @("OPUS_MODEL=$($sel.Opus)", "SONNET_MODEL=$($sel.Sonnet)", "HAIKU_MODEL=$($sel.Haiku)")
    Write-Host "==> profile '$($sel.Name)' configured (model tiers remapped to fit this machine)"
    Write-Host ""
    $dl = Read-Host "Download the models now (resumable any time)? [Y/n]"
    if ($dl -notmatch "^[Nn]") {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\download-models.ps1")
    }
    $fp = Read-Host "Install the full platform now (engines + local model serving)? [Y/n]"
    if ($fp -notmatch "^[Nn]") {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bin\oracle.ps1") setup
        Write-Host "==> start serving + chat any time: desktop shortcut -> or bin\oracle.ps1 serve / claude / desk"
    }
    Write-Host ""
    Write-Host "=============================================================="
    Write-Host " Setup complete - this machine now runs the full platform."
    Write-Host "   Desktop shortcut 'SentiVue Oracle' -> menu (serve, chat, desk, vault)"
    Write-Host "=============================================================="
    Read-Host "Press Enter to close"
    exit 0
} catch {
    Write-Host ""
    Write-Host "SETUP ERROR: $($_.Exception.Message)"
    Write-Host $_.ScriptStackTrace
    Write-Host ""
    Write-Host "Nothing destructive happened. Re-run the installer after fixing the above."
    Read-Host "Press Enter to close"
    exit 1
}
'@

$cmdHeader = @(
'@echo off'
'setlocal'
'title SentiVue Oracle Setup'
'set "ORACLE_SETUP_SELF=%~f0"'
"powershell -NoProfile -ExecutionPolicy Bypass -Command `"`$s=[IO.File]::ReadAllText(`$env:ORACLE_SETUP_SELF); `$a='#=='+'PSPAYLOAD'+'==#'; `$b='#=='+'B64PAYLOAD'+'==#'; iex (((`$s -split `$a)[1]) -split `$b)[0]`""
'if errorlevel 1 ('
'  echo.'
'  echo Setup did not finish. Review the message above ^(window stays open^).'
'  pause'
')'
'endlocal'
'exit /b'
'#==PSPAYLOAD==#'
)
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($zipball))
$chunks = [regex]::Matches($b64, ".{1,76}") | ForEach-Object { $_.Value }
$cmdPath = Join-Path $OutDir "SentiVue-Oracle-Setup-$Version.cmd"
$content = (($cmdHeader -join "`r`n") + "`r`n" + $psPayload + "`r`n#==B64PAYLOAD==#`r`n" + ($chunks -join "`r`n") + "`r`n")
[IO.File]::WriteAllText($cmdPath, $content, [Text.Encoding]::ASCII)
Write-Host ("==> {0}  ({1:N1} MB)" -f (Split-Path -Leaf $cmdPath), ((Get-Item $cmdPath).Length / 1MB))

Remove-Item $tarball, $zipball
Write-Host "installers ready in $OutDir"
