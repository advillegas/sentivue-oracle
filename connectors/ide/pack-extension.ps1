# pack-extension.ps1 - build oracle-agents.vsix from connectors\ide\oracle-agents.
# A bare folder copied into .vscode-oss\extensions is IGNORED (VSCodium tracks
# installs in extensions.json), so the extension must ship as a real .vsix and
# go through `codium --install-extension`. Uses bsdtar (ships with Windows 10+)
# for a spec-correct zip with forward-slash entries.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Src = Join-Path $PSScriptRoot "oracle-agents"
$OutDir = Join-Path $Root "incoming\vsix"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$ver = (Get-Content (Join-Path $Src "package.json") -Raw | ConvertFrom-Json).version
$stage = Join-Path $env:TEMP "oracle-agents-vsix"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $stage "extension\media") | Out-Null


# keep the manifest's version in lockstep with package.json
(Get-Content (Join-Path $Src "extension.vsixmanifest") -Raw) `
    -replace 'Id="oracle-agents" Version="[^"]+"', "Id=`"oracle-agents`" Version=`"$ver`"" |
    Set-Content -Path (Join-Path $stage "extension.vsixmanifest")
@'
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Default Extension="md" ContentType="text/markdown"/>
</Types>
'@ | Set-Content -LiteralPath (Join-Path $stage "[Content_Types].xml") -Encoding UTF8
Copy-Item (Join-Path $Src "package.json") (Join-Path $stage "extension")
Copy-Item (Join-Path $Src "extension.js") (Join-Path $stage "extension")
Copy-Item (Join-Path $Src "media\oracle.svg") (Join-Path $stage "extension\media")

$vsix = Join-Path $OutDir "sentivue.oracle-agents-$ver.vsix"
Remove-Item $vsix -Force -ErrorAction SilentlyContinue
Push-Location $stage
& tar.exe --format=zip -cf $vsix "[Content_Types].xml" "extension.vsixmanifest" "extension"
Pop-Location
if (-not (Test-Path $vsix)) { Write-Host "ERROR: vsix packaging failed"; exit 1 }
Write-Host "packed $vsix"
