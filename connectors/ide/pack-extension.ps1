# pack-extension.ps1 - build oracle-agents.vsix from connectors\ide\oracle-agents.
# A bare folder copied into .vscode-oss\extensions is IGNORED (VSCodium tracks
# installs in extensions.json), so the extension must ship as a real .vsix and
# go through `codium --install-extension`. Uses bsdtar (ships with Windows 10+)
# for a spec-correct zip with forward-slash entries.
param([string]$OutDir)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Src = Join-Path $PSScriptRoot "oracle-agents"
if (-not $OutDir) { $OutDir = Join-Path $Root ".tools\vscodium\vsix" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

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

$ver = (Get-Content (Join-Path $Src "package.json") -Raw | ConvertFrom-Json).version
$stage = Join-Path $env:TEMP ("oracle-agents-vsix-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path (Join-Path $stage "extension\media") | Out-Null


# keep the manifest's version in lockstep with package.json
$manifest = (Get-Content (Join-Path $Src "extension.vsixmanifest") -Raw) `
    -replace 'Id="oracle-agents" Version="[^"]+"', "Id=`"oracle-agents`" Version=`"$ver`""
Write-Utf8NoBomAtomic (Join-Path $stage "extension.vsixmanifest") $manifest
$contentTypes = @'
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Default Extension="css" ContentType="text/css"/>
  <Default Extension="md" ContentType="text/markdown"/>
</Types>
'@
Write-Utf8NoBomAtomic (Join-Path $stage "[Content_Types].xml") $contentTypes
Copy-Item (Join-Path $Src "package.json") (Join-Path $stage "extension")
Copy-Item (Join-Path $Src "extension.js") (Join-Path $stage "extension")
Copy-Item (Join-Path $Src "media\*") (Join-Path $stage "extension\media") -Recurse -Force

$vsix = Join-Path $OutDir "sentivue.oracle-agents-$ver.vsix"
Remove-Item $vsix -Force -ErrorAction SilentlyContinue
Push-Location $stage
try {
    & tar.exe --format=zip -cf $vsix "[Content_Types].xml" "extension.vsixmanifest" "extension"
    if ($LASTEXITCODE -ne 0) { throw "VSIX archive command failed" }
} finally {
    Pop-Location
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path $vsix)) { Write-Host "ERROR: vsix packaging failed"; exit 1 }
Write-Host "packed $vsix"
