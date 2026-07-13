# Repo-local, air-gapped LeanCTX runtime defaults.
$LeanCtxRoot = if ($env:ORACLE_ROOT) {
    $env:ORACLE_ROOT
} else {
    Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$env:ORACLE_ROOT = $LeanCtxRoot
$env:ORACLE_PROJECT_ROOT = if ($env:ORACLE_PROJECT_ROOT) {
    $env:ORACLE_PROJECT_ROOT
} else {
    (Get-Location).Path
}
$env:LEAN_CTX_CONFIG_DIR = Join-Path $LeanCtxRoot "state\lean-ctx\config"
$env:LEAN_CTX_DATA_DIR = Join-Path $LeanCtxRoot "state\lean-ctx\data"
$env:LEAN_CTX_STATE_DIR = Join-Path $LeanCtxRoot "state\lean-ctx\state"
$env:LEAN_CTX_CACHE_DIR = Join-Path $LeanCtxRoot "state\lean-ctx\cache"
$env:LEAN_CTX_PROJECT_ROOT = $env:ORACLE_PROJECT_ROOT
$env:LEAN_CTX_TOOL_PROFILE = "minimal"
$env:LEAN_CTX_DISABLED_TOOLS = "ctx_call"
$env:LEAN_CTX_NO_UPDATE_CHECK = "1"
$env:LEAN_CTX_AUTONOMY = "false"
$env:LEAN_CTX_NO_HOOK = "1"
$env:LEAN_CTX_RULES_INJECTION = "off"
