#!/usr/bin/env bash
# install-loop-eng.sh - loop-engineering toolkit (patterns + CLIs), mac twin.
#   bash harness/loop-engineering/install-loop-eng.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENDOR="$ROOT/harness/loop-engineering/vendor"
source "$ROOT/VERSIONS.lock"

if [[ ! -d "$VENDOR/.git" ]]; then
  echo "==> cloning loop-engineering ${LOOP_ENG_PIN} (shallow, pinned)"
  git clone --depth 1 --branch "$LOOP_ENG_PIN" "$LOOP_ENG_REPO" "$VENDOR"
else
  echo "==> loop-engineering vendor checkout present"
fi

echo "==> loop CLIs (pinned, repo-local npm prefix)"
export npm_config_prefix="$ROOT/.tools/npm"
mkdir -p "$npm_config_prefix"
npm install -g --no-audit --no-fund \
  "@cobusgreyling/loop-audit@${LOOP_AUDIT_NPM}" \
  "@cobusgreyling/loop-init@${LOOP_INIT_NPM}" \
  "@cobusgreyling/loop-cost@${LOOP_COST_NPM}" \
  "@cobusgreyling/loop-sync@${LOOP_SYNC_NPM}" >/dev/null

bash "$ROOT/bootstrap/sync-skills.sh"
echo "==> loop-engineering installed: patterns in harness/loop-engineering/vendor, CLIs in .tools/npm"
echo "    try: oracle loops audit"
