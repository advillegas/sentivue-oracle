# Local git vault - Windows-native twin of bootstrap/vault.sh.
# A private, offline "origin": history-protected bare git repositories under
# $env:ORACLE_VAULT (default %USERPROFILE%\oracle-git-vault). Zero dependencies
# beyond git. Same commands on every node of the ecosystem:
#
#   powershell -File bootstrap\vault.ps1 init                create vault + register 'vault' remote + first push
#   powershell -File bootstrap\vault.ps1 sync [repoPath]     push --all --tags (default: this repo); auto-creates bare repo
#   powershell -File bootstrap\vault.ps1 new <name>          empty bare repo for a new project
#   powershell -File bootstrap\vault.ps1 clone <name> [dest] clone a project out of the vault
#   powershell -File bootstrap\vault.ps1 list                repos, branches, last activity, sizes
#   powershell -File bootstrap\vault.ps1 backup [destDir]    tar the whole vault for external rotation
#
# Vault repos refuse deletes and non-fast-forward pushes (append-only history).
# To intentionally allow a rewrite on one repo:
#   git -C "$env:ORACLE_VAULT\<name>.git" config receive.denyNonFastForwards false
param(
    [Parameter(Position = 0)][string]$Cmd = "help",
    [Parameter(Position = 1)][string]$Arg1 = "",
    [Parameter(Position = 2)][string]$Arg2 = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Vault = if ($env:ORACLE_VAULT) { $env:ORACLE_VAULT } else { Join-Path $env:USERPROFILE "oracle-git-vault" }

function Bare-Init([string]$name) {
    $path = Join-Path $Vault "$name.git"
    if (-not (Test-Path $path)) {
        git init --bare --initial-branch=main --quiet $path | Out-Null
        git -C $path config receive.denyDeletes true
        git -C $path config receive.denyNonFastForwards true
        Write-Host "  created $path (history-protected)"
    }
    return $path
}

function Ensure-Remote([string]$repo, [string]$name) {
    $bare = Bare-Init $name
    # NB: probing with 'git remote get-url vault 2>$null' is a trap under
    # ErrorActionPreference=Stop (git's stderr becomes a terminating exception);
    # 'git remote' lists names with no stderr, so it probes safely.
    $remotes = @(git -C $repo remote)
    if ($remotes -contains "vault") { git -C $repo remote set-url vault $bare }
    else { git -C $repo remote add vault $bare }
}

switch ($Cmd) {
    "init" {
        New-Item -ItemType Directory -Force -Path $Vault | Out-Null
        Ensure-Remote $Root "sentivue-oracle"
        git -C $Root push --quiet vault --all
        git -C $Root push --quiet vault --tags
        Write-Host "vault ready: $Vault"
        Write-Host "  remote 'vault' registered on $Root; all branches + tags pushed"
    }
    "sync" {
        $repo = if ($Arg1) { (Resolve-Path $Arg1).Path } else { $Root }
        if (-not (Test-Path (Join-Path $repo ".git"))) { Write-Host "ERROR: $repo is not a git repo"; exit 1 }
        New-Item -ItemType Directory -Force -Path $Vault | Out-Null
        Ensure-Remote $repo (Split-Path -Leaf $repo)
        git -C $repo push vault --all
        git -C $repo push --quiet vault --tags
        Write-Host "synced $(Split-Path -Leaf $repo) -> vault"
    }
    "new" {
        if (-not $Arg1) { Write-Host "usage: vault.ps1 new <name>"; exit 1 }
        New-Item -ItemType Directory -Force -Path $Vault | Out-Null
        $bare = Bare-Init $Arg1
        Write-Host "clone with: git clone `"$bare`""
    }
    "clone" {
        if (-not $Arg1) { Write-Host "usage: vault.ps1 clone <name> [dest]"; exit 1 }
        $dest = if ($Arg2) { $Arg2 } else { $Arg1 }
        git clone (Join-Path $Vault "$Arg1.git") $dest
    }
    "list" {
        if (-not (Test-Path $Vault)) { Write-Host "no vault yet - run: vault.ps1 init"; exit 0 }
        Get-ChildItem $Vault -Directory -Filter "*.git" | ForEach-Object {
            $name = $_.Name -replace "\.git$", ""
            $last = git -C $_.FullName for-each-ref --count=1 --sort=-committerdate --format="%(committerdate:short) %(refname:short)" refs/heads 2>$null
            $nbr = @(git -C $_.FullName for-each-ref refs/heads 2>$null).Count
            $mb = [math]::Round((Get-ChildItem $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
            if (-not $last) { $last = "empty" }
            Write-Host ("  {0,-24} {1,3} branches  {2,8} MB  last: {3}" -f $name, $nbr, $mb, $last)
        }
    }
    "backup" {
        if (-not (Test-Path $Vault)) { Write-Host "no vault to back up"; exit 1 }
        $dest = if ($Arg1) { $Arg1 } else { Join-Path $Root "backups" }
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $out = Join-Path $dest ("vault-{0}.tar.gz" -f (Get-Date -Format "yyyyMMdd-HHmm"))
        tar -czf $out -C (Split-Path -Parent $Vault) (Split-Path -Leaf $Vault)
        $mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
        Write-Host "vault backup: $out ($mb MB) - rotate to external media"
    }
    default {
        Get-Content $PSCommandPath | Select-Object -Skip 1 -First 15 | ForEach-Object { $_ -replace "^# ?", "" }
    }
}
