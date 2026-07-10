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

# ============================== macOS .command ==============================
$macLines = @(
'#!/bin/bash'
'# SentiVue Oracle installer - double-click to run. If macOS blocks it'
'# (downloaded file), right-click the file and choose Open the first time.'
'set -euo pipefail'
'clear'
'echo "=============================================================="'
'echo "        SentiVue Oracle - guided installation"'
'echo "  Offline agentic workstation: local models, dual engines,"'
'echo "  git vault, mission conductor, controlled-internet envoy."'
'echo "=============================================================="'
'echo'
'DEFAULT="$HOME/sentivue-oracle"'
'read -r -p "Install location [$DEFAULT]: " DEST'
'DEST="${DEST:-$DEFAULT}"'
'if [[ -e "$DEST/README.md" ]]; then'
'  read -r -p "$DEST already exists. Overwrite files with this version? [y/N] " OK'
'  [[ "${OK:-n}" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }'
'fi'
'mkdir -p "$DEST"'
'echo "==> unpacking payload..."'
'PAYLOAD_LINE=$(awk "/^__PAYLOAD_BELOW__$/ {print NR + 1; exit 0}" "$0")'
'tail -n +"$PAYLOAD_LINE" "$0" | tar -xz --strip-components=1 -C "$DEST"'
'chmod +x "$DEST/install" "$DEST/bin/"* "$DEST/bootstrap/"*.sh "$DEST/serving/service.sh" 2>/dev/null || true'
'echo "==> unpacked to $DEST"'
'echo'
'echo "The guided installer will now walk you through:"'
'echo "  preflight checks -> tools -> model profile choice -> downloads -> verify"'
'echo "Every phase is resumable: re-run $DEST/install any time."'
'echo'
'read -r -p "Start guided installation now? [Y/n] " GO'
'if [[ ! "${GO:-y}" =~ ^[Nn]$ ]]; then'
'  cd "$DEST" && exec bash install'
'else'
'  echo "When ready:  cd $DEST && bash install"'
'fi'
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
Write-Host "=============================================================="
Write-Host "        SentiVue Oracle - Windows node setup"
Write-Host "  Authoring tools: model pre-downloader + local git vault."
Write-Host "  (The full appliance runs on the Mac - use the .command file)"
Write-Host "=============================================================="
Write-Host ""
$default = Join-Path $env:USERPROFILE "sentivue-oracle"
$dest = Read-Host "Install location [$default]"
if (-not $dest) { $dest = $default }
if (Test-Path (Join-Path $dest "README.md")) {
    $ok = Read-Host "$dest already exists. Overwrite files with this version? [y/N]"
    if ($ok -notmatch "^[Yy]$") { Write-Host "Cancelled."; exit 0 }
}
Write-Host "==> unpacking payload..."
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
Write-Host "==> unpacked to $dest"
Write-Host ""
$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if ($hasGit) {
    $v = Read-Host "Initialize the local git vault (private offline backup remote)? [Y/n]"
    if ($v -notmatch "^[Nn]$") {
        if (-not (Test-Path (Join-Path $dest ".git"))) { git -C $dest init -b main | Out-Null; git -C $dest add -A; git -C $dest -c user.name="oracle" -c user.email="oracle@localhost" commit -q -m "installer import" }
        & powershell -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\vault.ps1") init
    }
} else {
    Write-Host "NOTE: git not found - install Git for Windows, then run bin\oracle.ps1 vault init"
}
Write-Host ""
Write-Host "Model pre-download (transfers to the Mac later; resumable any time):"
Write-Host "  1) full    ~700 GB   2) coder ~315 GB   3) minimal ~40 GB   4) skip"
$m = Read-Host "choose [4]"
$names = @{ "2" = @("qwen3-coder-480b","qwen3-coder-30b","qwen3-embedding-4b"); "3" = @("qwen3-coder-30b","qwen3-embedding-4b") }
if ($m -eq "1" -or $m -eq "2" -or $m -eq "3") {
    if ($names.ContainsKey($m)) { Set-Content -Path (Join-Path $dest "serving\models.profile") -Value ($names[$m] -join "`n") }
    & powershell -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\download-models.ps1")
}
Write-Host ""
Write-Host "=============================================================="
Write-Host " Setup complete."
Write-Host "   Everything else:   $dest\bin\oracle.ps1  (vault / models / finish)"
Write-Host "   Mac deployment:    use SentiVue-Oracle-Installer-*.command there"
Write-Host "=============================================================="
Read-Host "Press Enter to close"
'@

$cmdHeader = @(
'@echo off'
'title SentiVue Oracle Setup'
'set "ORACLE_SETUP_SELF=%~f0"'
"powershell -NoProfile -ExecutionPolicy Bypass -Command `"`$s=[IO.File]::ReadAllText(`$env:ORACLE_SETUP_SELF); `$a='#=='+'PSPAYLOAD'+'==#'; `$b='#=='+'B64PAYLOAD'+'==#'; iex (((`$s -split `$a)[1]) -split `$b)[0]`""
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
