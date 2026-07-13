#!/usr/bin/env bash
# Compatibility entry point for the shared profile-aware offline verifier.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$ROOT/serving/service.sh" verify --include-engines "$@"
