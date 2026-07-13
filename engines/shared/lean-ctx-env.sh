#!/usr/bin/env bash
# Repo-local, air-gapped LeanCTX runtime defaults.
LEAN_CTX_ROOT="${ORACLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export ORACLE_ROOT="$LEAN_CTX_ROOT"
export ORACLE_PROJECT_ROOT="${ORACLE_PROJECT_ROOT:-$PWD}"
export LEAN_CTX_CONFIG_DIR="$LEAN_CTX_ROOT/state/lean-ctx/config"
export LEAN_CTX_DATA_DIR="$LEAN_CTX_ROOT/state/lean-ctx/data"
export LEAN_CTX_STATE_DIR="$LEAN_CTX_ROOT/state/lean-ctx/state"
export LEAN_CTX_CACHE_DIR="$LEAN_CTX_ROOT/state/lean-ctx/cache"
export LEAN_CTX_PROJECT_ROOT="$ORACLE_PROJECT_ROOT"
export LEAN_CTX_TOOL_PROFILE=minimal
export LEAN_CTX_DISABLED_TOOLS=ctx_call
export LEAN_CTX_NO_UPDATE_CHECK=1
export LEAN_CTX_AUTONOMY=false
export LEAN_CTX_NO_HOOK=1
export LEAN_CTX_RULES_INJECTION=off
