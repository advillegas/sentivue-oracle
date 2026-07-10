#!/usr/bin/env bash
# ensure-tools.sh - self-provisioning toolbelt healer (macOS twin).
# Doctrine: a missing tool is a task, not a blocker. Idempotent, best-effort.
# On a hardened (air-gapped) machine, installs that need the network are queued
# as NET-REQUESTS instead of failing silently.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXED=()

online() { curl -sf -m 5 https://formulae.brew.sh >/dev/null 2>&1; }
queue_request() {  # queue_request <artifact> <why>
  mkdir -p "$ROOT/memory"
  echo "- [ ] $(date +%F) ensure-tools: NEED $1 — WHY $2 — USED-IN platform toolbelt" >> "$ROOT/memory/NET-REQUESTS.md"
  echo "    (air-gapped: queued NET-REQUEST for $1)"
}

# ---- brew ----------------------------------------------------------------------
if ! command -v brew >/dev/null; then
  echo "WARN: Homebrew missing — run bootstrap/install.sh first"; exit 0
fi

# ---- uv + python env ------------------------------------------------------------
if ! command -v uv >/dev/null; then
  if online; then echo "==> uv missing - brew install uv"; brew install uv && FIXED+=("uv")
  else queue_request "brew:uv" "python env manager"; fi
fi

# ---- jq (config surgery) ---------------------------------------------------------
if ! command -v jq >/dev/null; then
  if online; then echo "==> jq missing - brew install jq"; brew install jq && FIXED+=("jq")
  else queue_request "brew:jq" "config generation"; fi
fi

# ---- node + engines (pinned, repo-local) -----------------------------------------
if ! command -v node >/dev/null; then
  if online; then echo "==> node missing - brew install node"; brew install node && FIXED+=("node")
  else queue_request "brew:node" "engine runtime"; fi
fi
if command -v npm >/dev/null && { [[ ! -x "$ROOT/.tools/npm/bin/claude" ]] || [[ ! -x "$ROOT/.tools/npm/bin/opencode" ]]; }; then
  CC_V="$(sed -n 's/^CLAUDE_CODE_NPM_VERSION=\([^#]*\).*/\1/p' "$ROOT/VERSIONS.lock" | xargs)"
  OC_V="$(sed -n 's/^OPENCODE_NPM_VERSION=\([^#]*\).*/\1/p' "$ROOT/VERSIONS.lock" | xargs)"
  if online; then
    echo "==> engines missing - npm install (pinned, repo-local)"
    NPM_CONFIG_PREFIX="$ROOT/.tools/npm" npm install -g \
      "@anthropic-ai/claude-code@$CC_V" "opencode-ai@$OC_V" && FIXED+=("engines")
  else
    queue_request "npm:@anthropic-ai/claude-code@$CC_V + npm:opencode-ai@$OC_V" "engines"
  fi
fi

# ---- pytest (inside the project env) ----------------------------------------------
if command -v uv >/dev/null && [[ -d "$ROOT/env" ]]; then
  if ! uv run --project "$ROOT/env" python -c "import pytest" >/dev/null 2>&1; then
    if online; then
      echo "==> pytest missing - uv add (pinned to major 8)"
      (cd "$ROOT/env" && uv add "pytest>=8,<9") && FIXED+=("pytest")
    else
      queue_request "pip:pytest>=8,<9" "mission deterministic checks"
    fi
  fi
fi

[[ ${#FIXED[@]} -gt 0 ]] && echo "==> self-provisioned: ${FIXED[*]}"
echo "TOOLS: uv $(command -v uv >/dev/null && echo OK || echo MISSING) | jq $(command -v jq >/dev/null && echo OK || echo MISSING) | node $(command -v node >/dev/null && node --version || echo MISSING) | engines $([[ -x "$ROOT/.tools/npm/bin/claude" ]] && echo OK || echo MISSING)"
