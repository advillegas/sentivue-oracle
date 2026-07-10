# sync-models.ps1 - auto-detect the models actually on this machine and point
# every IDE surface at them. Safe to run any time; runs on every IDE launch.
#
# Detection order (first that answers wins):
#   1. live llama-swap  GET http://127.0.0.1:9099/v1/models
#   2. disk scan        models\<name>\**\*.gguf (anything downloaded is real)
#
# Writes:
#   ~\.continue\config.yaml      one entry per detected model, roles by slot
#   <root>\state\roo-import.json Roo Code provider profile (auto-imported on startup
#                                via the roo-cline.autoImportSettingsPath setting)
#   <root>\serving\tiers.env     opus/sonnet/haiku remapped onto models that exist
#                                (engine launchers + conductor read this)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ApiBase = "http://127.0.0.1:9099/v1"

# ---- manifest metadata (slot + context per model) ---------------------------
$Meta = @{}
Get-Content (Join-Path $Root "serving\models.manifest") |
    Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object {
        $f = $_ -split "\|"
        $Meta[$f[0].Trim()] = @{ Slot = $f[3].Trim(); Ctx = [int]$f[4].Trim() }
    }

# ---- detect -----------------------------------------------------------------
$ids = $null
$source = ""
try {
    $ids = @((Invoke-RestMethod -Uri "$ApiBase/models" -TimeoutSec 3).data | ForEach-Object { $_.id })
    if ($ids.Count -gt 0) { $source = "live llama-swap" } else { $ids = $null }
} catch { $ids = $null }

if (-not $ids) {
    # anything with a .gguf on disk was downloaded on purpose - serve it
    $ids = @($Meta.Keys | Where-Object {
        Get-ChildItem (Join-Path $Root "models\$_") -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue | Select-Object -First 1
    })
    if ($ids.Count -gt 0) { $source = "disk scan" }
}

if (-not $ids -or $ids.Count -eq 0) {
    Write-Host "sync-models: no models detected (download models first) - IDE configs left untouched"
    exit 0
}

# ---- tier mapping onto what actually exists ---------------------------------
function Resolve-Tier([string]$Wanted, [string[]]$Pool) {
    if ($Wanted -and $Pool -contains $Wanted) { return $Wanted }
    return $null
}
$chat = @($ids | Where-Object { -not $Meta.ContainsKey($_) -or $Meta[$_].Slot -ne "embed" })
$fast = @($ids | Where-Object { $Meta.ContainsKey($_) -and $Meta[$_].Slot -eq "fast" })
$big  = @($ids | Where-Object { $Meta.ContainsKey($_) -and $Meta[$_].Slot -eq "big" })
$tiersFile = Join-Path $Root "serving\tiers.env"
$want = @{}
if (Test-Path $tiersFile) {
    Get-Content $tiersFile | ForEach-Object { $kv = $_ -split "=", 2; if ($kv.Count -eq 2) { $want[$kv[0].Trim()] = $kv[1].Trim() } }
}
$sonnet = Resolve-Tier $want["SONNET_MODEL"] $chat
if (-not $sonnet) { $sonnet = ($fast + $big + $chat) | Select-Object -First 1 }
$opus = Resolve-Tier $want["OPUS_MODEL"] $chat
if (-not $opus) { $opus = ($big + @($sonnet)) | Select-Object -First 1 }
$haiku = Resolve-Tier $want["HAIKU_MODEL"] $chat
if (-not $haiku) { $haiku = ($fast + @($sonnet)) | Select-Object -First 1 }

# anchor = default chat model everywhere; never an embedding model
$anchor = $sonnet
if (-not $anchor) { $anchor = $ids[0] }
$ordered = @($anchor) + @($ids | Where-Object { $_ -ne $anchor })

# ---- tiers.env + engine configs: remap tiers onto models that exist ---------
if ($sonnet) {
    Set-Content -Path $tiersFile -Value (@("OPUS_MODEL=$opus", "SONNET_MODEL=$sonnet", "HAIKU_MODEL=$haiku") -join "`n")
    $cs = Join-Path $Root "engines\claude-code\home\settings.json"
    if (Test-Path $cs) {
        try {
            $j = Get-Content $cs -Raw | ConvertFrom-Json
            $j.env | Add-Member -NotePropertyName "ANTHROPIC_MODEL" -NotePropertyValue $sonnet -Force
            $j.env | Add-Member -NotePropertyName "ANTHROPIC_DEFAULT_OPUS_MODEL" -NotePropertyValue $opus -Force
            $j.env | Add-Member -NotePropertyName "ANTHROPIC_DEFAULT_SONNET_MODEL" -NotePropertyValue $sonnet -Force
            $j.env | Add-Member -NotePropertyName "ANTHROPIC_DEFAULT_HAIKU_MODEL" -NotePropertyValue $haiku -Force
            $j.env | Add-Member -NotePropertyName "ANTHROPIC_SMALL_FAST_MODEL" -NotePropertyValue $haiku -Force
            $j | Add-Member -NotePropertyName "model" -NotePropertyValue $sonnet -Force
            ConvertTo-Json -InputObject $j -Depth 20 | Set-Content -Path $cs
        } catch { Write-Host "WARN: could not patch claude settings.json: $($_.Exception.Message)" }
    }
    $oc = Join-Path $Root "engines\opencode\xdg\opencode\opencode.json"
    if (Test-Path $oc) {
        try {
            $j = Get-Content $oc -Raw | ConvertFrom-Json
            # OpenCode only offers models declared in the provider map - rebuild it
            # from the detected chat models so the picker matches the machine.
            $modelsMap = [ordered]@{}
            foreach ($id in $chat) {
                $ctx = 32768
                if ($Meta.ContainsKey($id) -and $Meta[$id].Ctx -gt 0) { $ctx = $Meta[$id].Ctx }
                $out = [Math]::Min([Math]::Max([int][Math]::Floor($ctx / 2), 8192), 65536)
                $entry = [ordered]@{ name = "$id (local)"; tool_call = $true }
                if ($id -match "thinking") { $entry["reasoning"] = $true }
                $entry["limit"] = [ordered]@{ context = $ctx; output = $out }
                $modelsMap[$id] = $entry
            }
            $j.provider.oracle | Add-Member -NotePropertyName "models" -NotePropertyValue $modelsMap -Force
            $j | Add-Member -NotePropertyName "model" -NotePropertyValue "oracle/$sonnet" -Force
            $j | Add-Member -NotePropertyName "small_model" -NotePropertyValue "oracle/$haiku" -Force
            ConvertTo-Json -InputObject $j -Depth 20 | Set-Content -Path $oc
        } catch { Write-Host "WARN: could not patch opencode.json: $($_.Exception.Message)" }
    }
    $adv = Join-Path $Root "engines\opencode\xdg\opencode\agent\adversary.md"
    if (Test-Path $adv) {
        (Get-Content $adv) -replace "^model: oracle/.*", "model: oracle/$opus" | Set-Content $adv
    }
} else {
    Write-Host "sync-models: WARNING - only embedding models found; download a chat model"
}

# ---- Continue: ~\.continue\config.yaml --------------------------------------
$yaml = @(
    "# GENERATED by sync-models.ps1 - models auto-detected from $source."
    "# Regenerated on every IDE launch; edit serving\models.profile to change the set."
    "name: SentiVue Oracle"
    "version: 1.0.0"
    "models:"
)
foreach ($id in $ordered) {
    $slot = "custom"; $ctx = 0
    if ($Meta.ContainsKey($id)) { $slot = $Meta[$id].Slot; $ctx = $Meta[$id].Ctx }
    $roles = switch ($slot) {
        "fast"  { "[chat, edit, apply, autocomplete]" }
        "embed" { "[embed]" }
        default { "[chat, edit, apply]" }
    }
    $yaml += "  - name: $id (local)"
    $yaml += "    provider: openai"
    $yaml += "    model: $id"
    $yaml += "    apiBase: $ApiBase"
    $yaml += "    apiKey: oracle-local"
    $yaml += "    roles: $roles"
    if ($ctx -gt 0) {
        $yaml += "    defaultCompletionOptions:"
        $yaml += "      contextLength: $ctx"
    }
}
$cont = Join-Path $env:USERPROFILE ".continue"
New-Item -ItemType Directory -Force -Path $cont | Out-Null
Set-Content -Path (Join-Path $cont "config.yaml") -Value ($yaml -join "`n")

# ---- Roo Code: provider profile auto-import file ----------------------------
$ctxAnchor = 32768
if ($Meta.ContainsKey($anchor) -and $Meta[$anchor].Ctx -gt 0) { $ctxAnchor = $Meta[$anchor].Ctx }
$roo = @{
    providerProfiles = @{
        currentApiConfigName = "SentiVue Oracle (local)"
        apiConfigs = @{
            "SentiVue Oracle (local)" = @{
                id                   = "sentivue-local"
                apiProvider          = "openai"
                openAiBaseUrl        = $ApiBase
                openAiApiKey         = "oracle-local"
                openAiModelId        = $anchor
                openAiStreamingEnabled = $true
                openAiCustomModelInfo = @{
                    maxTokens          = -1
                    contextWindow      = $ctxAnchor
                    supportsImages     = $false
                    supportsPromptCache = $false
                }
            }
        }
    }
}
$state = Join-Path $Root "state"
New-Item -ItemType Directory -Force -Path $state | Out-Null
ConvertTo-Json -InputObject $roo -Depth 10 | Set-Content -Path (Join-Path $state "roo-import.json")

Write-Host "sync-models: $($ordered.Count) model(s) from $source (opus=$opus sonnet=$sonnet haiku=$haiku)"
Write-Host "  $($ordered -join ', ')"
