# sync-models.ps1 - auto-detect the models actually on this machine and point
# every IDE surface at them. Safe to run any time; runs on every IDE launch.
#
# Detection order (first that answers wins):
#   1. live llama-swap  GET http://127.0.0.1:9099/v1/models
#   2. disk scan        models\<name>\**\*.gguf (anything downloaded is real)
#
# Writes:
#   ~\.continue\config.yaml      one entry per detected model, roles by slot
#   ~\.config\kilo\kilo.jsonc    Kilo Code global config (local provider + models,
#                                telemetry off, sharing off)
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
# Preference order per tier: models the install profile intended, then slot
# fit, then anything chat-capable. A stale tiers.env never pins a tier to a
# lesser model once the intended one shows up on disk.
$chat = @($ids | Where-Object { -not $Meta.ContainsKey($_) -or $Meta[$_].Slot -ne "embed" })
$tiersFile = Join-Path $Root "serving\tiers.env"
$want = @{}
if (Test-Path $tiersFile) {
    Get-Content $tiersFile | ForEach-Object { $kv = $_ -split "=", 2; if ($kv.Count -eq 2) { $want[$kv[0].Trim()] = $kv[1].Trim() } }
}
$profileSet = @()
$profileFile = Join-Path $Root "serving\models.profile"
if (Test-Path $profileFile) {
    $profileSet = @(Get-Content $profileFile | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") } | ForEach-Object { $_.Trim() })
}
function Pick-Tier([string]$Wanted, [string[]]$SlotPref) {
    # honor tiers.env only when it points at a profile-intended (or unprofiled) model
    if ($Wanted -and $chat -contains $Wanted -and ($profileSet.Count -eq 0 -or $profileSet -contains $Wanted)) { return $Wanted }
    foreach ($inProf in @($true, $false)) {
        foreach ($s in $SlotPref) {
            $hit = $chat | Where-Object {
                $slot = "custom"; if ($Meta.ContainsKey($_)) { $slot = $Meta[$_].Slot }
                ($slot -eq $s) -and (($profileSet -contains $_) -eq $inProf)
            } | Select-Object -First 1
            if ($hit) { return $hit }
        }
    }
    return ($chat | Select-Object -First 1)
}
$sonnet = Pick-Tier $want["SONNET_MODEL"] @("fast", "big")
$opus   = Pick-Tier $want["OPUS_MODEL"]   @("big", "fast")
# haiku = the smallest fast model that can HOLD AN AGENT SESSION (ctx >= 32k).
# A separate small process protects the primary model's KV prefix cache from
# background traffic - but a model too small for engine sessions is worse than
# eviction: the 16k 7B died on Claude Code's tool grammar AND its context floor
# (FAILURES 2026-07-11). No qualifying small model => haiku rides the sonnet
# model and cache eviction is the accepted cost.
$haiku = $null
$fastOnDisk = $chat | Where-Object {
    $Meta.ContainsKey($_) -and $Meta[$_].Slot -eq "fast" -and $Meta[$_].Ctx -ge 32768
} | ForEach-Object {
    $sz = (Get-ChildItem (Join-Path $Root "models\$_") -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum
    if ($sz) { [pscustomobject]@{ Id = $_; Size = $sz } }
} | Sort-Object Size
if ($fastOnDisk) { $haiku = ($fastOnDisk | Select-Object -First 1).Id }
if (-not $haiku) { $haiku = $sonnet }
if (-not $haiku) { $haiku = Pick-Tier $want["HAIKU_MODEL"] @("fast", "big") }

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
    # OpenCode agent personas: remap each role's model line onto this machine's tiers
    $agentTier = @{
        "researcher.md" = $haiku; "auditor.md" = $haiku; "librarian.md" = $haiku
        "developer.md" = $sonnet; "envoy.md" = $sonnet; "adversary.md" = $opus
    }
    $agentDir = Join-Path $Root "engines\opencode\xdg\opencode\agent"
    foreach ($kv in $agentTier.GetEnumerator()) {
        $f = Join-Path $agentDir $kv.Key
        if (Test-Path $f) {
            (Get-Content $f) -replace "^model: oracle/.*", "model: oracle/$($kv.Value)" | Set-Content $f
        }
    }
} else {
    Write-Host "sync-models: WARNING - only embedding models found; download a chat model"
}

# ---- Continue: ~\.continue\config.yaml --------------------------------------
# systemMessage grounds small local models: without it they fall back to 2022-era
# chatbot habits ("As an AI I cannot run commands..."). capabilities: [tool_use]
# unlocks Continue's Agent mode so the model can actually edit files + run
# terminal commands through the IDE.
$sysmsg = @(
    "You are SentiVue Oracle, a senior software engineer running 100% locally on the user's machine - private, offline, no cloud.",
    "It is 2026. Behave like a capable coding agent, not a chatbot.",
    "In Agent mode you have real tools: create and edit files, run terminal commands, read their output, and verify results yourself instead of instructing the user.",
    "Never claim you lack access to the machine. If tools are unavailable (plain Chat mode), give the exact code or command once - no tutorials, no 'go to python.org'.",
    "Style: direct and concise. No greetings, no apologies, never 'As an AI'. Bias to action: implement, run, verify, report. Make reasonable assumptions and state them in one line."
)
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
    if ($slot -ne "embed") {
        $yaml += "    capabilities: [tool_use]"
        $yaml += "    systemMessage: |"
        foreach ($l in $sysmsg) { $yaml += "      $l" }
    }
    if ($ctx -gt 0) {
        $yaml += "    defaultCompletionOptions:"
        $yaml += "      contextLength: $ctx"
    }
}
$cont = Join-Path $env:USERPROFILE ".continue"
New-Item -ItemType Directory -Force -Path $cont | Out-Null
Set-Content -Path (Join-Path $cont "config.yaml") -Value ($yaml -join "`n")

# ---- Kilo Code: global JSONC config (all Kilo surfaces read this) -----------
$kiloModels = [ordered]@{}
foreach ($id in $chat) {
    $ctx = 32768
    if ($Meta.ContainsKey($id) -and $Meta[$id].Ctx -gt 0) { $ctx = $Meta[$id].Ctx }
    $out = [Math]::Min([Math]::Max([int][Math]::Floor($ctx / 2), 8192), 65536)
    $entry = [ordered]@{ name = "$id (local)"; tool_call = $true }
    if ($id -match "thinking") { $entry["reasoning"] = $true }
    $entry["limit"] = [ordered]@{ context = $ctx; output = $out }
    $kiloModels[$id] = $entry
}
if ($kiloModels.Count -gt 0) {
    $kilo = [ordered]@{
        '$schema'           = "https://app.kilo.ai/config.json"
        model               = "openai-compatible/$anchor"
        share               = "disabled"
        enabled_providers   = @("openai-compatible")
        instructions        = @(
            (Join-Path $Root "engines\shared\IDE-AGENT.md"),
            (Join-Path $Root "engines\shared\CONVENTIONS.md"),
            (Join-Path $Root "engines\shared\AUTONOMY.md")
        )
        provider            = [ordered]@{
            "openai-compatible" = [ordered]@{
                options = [ordered]@{ apiKey = "oracle-local"; baseURL = $ApiBase }
                models  = $kiloModels
            }
        }
        # platform policy (same as OpenCode): work freely on local files/shell,
        # no direct network - envoy is the only fetch path. Required for headless
        # `kilo run --auto`, which only auto-approves what this config allows.
        permission          = [ordered]@{ edit = "allow"; bash = "allow"; webfetch = "deny" }
        experimental        = [ordered]@{ openTelemetry = $false }
    }
    $kiloDir = Join-Path $env:USERPROFILE ".config\kilo"
    New-Item -ItemType Directory -Force -Path $kiloDir | Out-Null
    $body = ConvertTo-Json -InputObject $kilo -Depth 20
    Set-Content -Path (Join-Path $kiloDir "kilo.jsonc") -Value ("// GENERATED by sync-models.ps1 - regenerated on every IDE launch`n" + $body)
}

Write-Host "sync-models: $($ordered.Count) model(s) from $source (opus=$opus sonnet=$sonnet haiku=$haiku)"
Write-Host "  $($ordered -join ', ')"
