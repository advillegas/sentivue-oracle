# Windows twin of sync-skills.sh: sync skills/ (source of truth) into both
# engines. Uses NTFS junctions (no admin rights needed) so edits and NEW skills
# flow through instantly; falls back to copying on non-NTFS destinations.
#   Claude Code: engines\claude-code\home\skills\<name>   (CLAUDE_CONFIG_DIR)
#   OpenCode:    engines\opencode\xdg\opencode\skill\<name>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$dests = @(
    (Join-Path $Root "engines\claude-code\home\skills"),
    (Join-Path $Root "engines\opencode\xdg\opencode\skill")
)
foreach ($d in $dests) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

$count = 0
foreach ($skill in Get-ChildItem (Join-Path $Root "skills") -Directory) {
    if (-not (Test-Path (Join-Path $skill.FullName "SKILL.md"))) { continue }
    foreach ($d in $dests) {
        $link = Join-Path $d $skill.Name
        if (Test-Path $link) { Remove-Item $link -Recurse -Force }
        try {
            New-Item -ItemType Junction -Path $link -Target $skill.FullName | Out-Null
        } catch {
            Copy-Item $skill.FullName $link -Recurse -Force
        }
    }
    $count++
}
Write-Host "Synced $count skills into both engines."
