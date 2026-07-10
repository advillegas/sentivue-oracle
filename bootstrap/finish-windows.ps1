# finish-windows.ps1 — one-shot: commit + package + create PRIVATE GitHub repo + push.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File C:\Users\Aaron\sentivue-oracle\bootstrap\finish-windows.ps1
param(
    [string]$RepoName = "sentivue-oracle",   # target GitHub repo name
    [switch]$SkipPush                        # commit + package only
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "==> repo root: $Root"

# ---- 1. git init + commit ---------------------------------------------------
if (-not (Test-Path "$Root\.git")) {
    git init -b main | Out-Host
}
git add -A
$hasIdentity = (git config user.email) -and (git config user.name)
if (-not $hasIdentity) {
    # Repo-local identity using the owner's GitHub no-reply address, so GitHub
    # attributes commits to the right account (email match drives the avatar).
    git config user.name "advillegas"
    git config user.email "74381111+advillegas@users.noreply.github.com"
}
$msg = "SentiVue Oracle: offline agentic workstation (Claude Code + OpenCode engines, guided installer)"
if (git status --porcelain) {
    git commit -m $msg | Out-Host
} else {
    Write-Host "==> nothing new to commit"
}
$sha = git log --oneline -1
Write-Host "==> HEAD: $sha"

# ---- 2. package -------------------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd"
$tarball = Join-Path (Split-Path -Parent $Root) "sentivue-oracle-$stamp.tar.gz"
Push-Location (Split-Path -Parent $Root)
tar -czf $tarball --exclude "sentivue-oracle/.git" sentivue-oracle
Pop-Location
$size = [math]::Round((Get-Item $tarball).Length / 1MB, 1)
Write-Host "==> tarball: $tarball ($size MB)"

# ---- 3. private repo + push -------------------------------------------------
if ($SkipPush) { Write-Host "==> push skipped (-SkipPush)"; exit 0 }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "==> gh CLI not installed — push skipped."
    Write-Host "    Install GitHub CLI, run 'gh auth login', then re-run this script."
    exit 0
}
gh auth status 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> gh not authenticated — run 'gh auth login' first, then re-run."
    exit 0
}
# PRIVACY: repo must be private, always.
$existing = gh repo view $RepoName --json url --jq .url 2>$null
if ($LASTEXITCODE -eq 0 -and $existing) {
    $vis = gh repo view $RepoName --json visibility --jq .visibility
    if ($vis -ne "PRIVATE") { Write-Host "==> ABORT: $existing exists but is $vis, not PRIVATE."; exit 1 }
    if (-not (git remote get-url origin 2>$null)) { git remote add origin $existing }
    git push -u origin main | Out-Host
    Write-Host "==> pushed to existing private repo: $existing"
} else {
    gh repo create $RepoName --private --source . --remote origin --push | Out-Host
    $url = gh repo view $RepoName --json url --jq .url
    Write-Host "==> created PRIVATE repo and pushed: $url"
}
Write-Host "==> DONE. Transfer the tarball (or 'git clone' on the Mac) and run: bash install"
