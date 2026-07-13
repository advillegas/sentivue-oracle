#!/usr/bin/env bash
# Compatibility entry point. Shared rendering lives in verification/serving.py
# and the platform service wrapper supplies the policy-bound backend/binary.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$ROOT/serving/service.sh" render "$@"
