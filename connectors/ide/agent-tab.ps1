# agent-tab.ps1 - one agent per terminal tab, Cursor-style.
# Opened by the IDE terminal profiles ("Oracle Agent: ...") or a keybinding;
# open as many tabs as you want - each is an independent engine session on the
# local models. -Worktree gives the agent its own git worktree so parallel
# agents never collide on files (merge back when you like the result).
#
#   agent-tab.ps1 claude              agent in the repo itself
#   agent-tab.ps1 claude -Worktree    agent in an isolated worktree + branch
#   agent-tab.ps1 opencode
#   agent-tab.ps1 kilo
param(
    [Parameter(Position = 0)][ValidateSet("claude", "opencode", "kilo")][string]$Engine = "claude",
    [switch]$Worktree
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$dir = $Root
$branch = ""
if ($Worktree) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $branch = "agent/tab-$stamp"
    $dir = Join-Path $Root ".worktrees\tab-$stamp"
    git -C $Root worktree add -b $branch $dir | Out-Null
}

$engineName = switch ($Engine) { "claude" { "Claude Code" } "opencode" { "OpenCode" } "kilo" { "Kilo Code" } }
Write-Host ""
Write-Host "  SentiVue Oracle agent tab - $engineName (local models)" -ForegroundColor Cyan
if ($branch) {
    Write-Host "  isolated worktree: $dir" -ForegroundColor DarkGray
    Write-Host "  branch: $branch  (merge back with: git merge $branch)" -ForegroundColor DarkGray
} else {
    Write-Host "  working directly in: $dir  (open a worktree tab for parallel isolation)" -ForegroundColor DarkGray
}
Write-Host ""

$launcher = switch ($Engine) {
    "claude"   { "engines\claude-code\launch.ps1" }
    "opencode" { "engines\opencode\launch.ps1" }
    "kilo"     { "engines\kilo\launch.ps1" }
}
$env:ORACLE_PROJECT_ROOT = $dir
Set-Location $dir
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root $launcher)

Write-Host ""
if ($branch) {
    Write-Host "  agent exited. keep:   git merge $branch" -ForegroundColor DarkGray
    Write-Host "          discard:  git worktree remove $dir; git branch -D $branch" -ForegroundColor DarkGray
} else {
    Write-Host "  agent exited - this shell stays open (rerun with the up arrow)" -ForegroundColor DarkGray
}
