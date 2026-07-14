#!/usr/bin/env bash
# Establish the pinned Python trust root using only macOS-native primitives.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${ORACLE_DEPENDENCY_CACHE:-$ROOT/incoming/dependency-cache}"
# shellcheck disable=SC1091
source "$ROOT/VERSIONS.lock"

URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250517/cpython-3.12.10%2B20250517-aarch64-apple-darwin-install_only.tar.gz"
[[ "$PYTHON_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "portable Python version policy is unresolved" >&2
  exit 1
}
[[ "$PYTHON_DARWIN_ARM64_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "portable Python digest policy is unresolved" >&2
  exit 1
}

mkdir -p "$CACHE"
partial="$CACHE/cpython-$PYTHON_VERSION-darwin-arm64.tar.gz.part"
archive="$CACHE/cpython-$PYTHON_VERSION-darwin-arm64.tar.gz"
if [[ -f "$archive" ]] &&
   [[ "$(shasum -a 256 "$archive" | awk '{print $1}')" != \
      "$PYTHON_DARWIN_ARM64_SHA256" ]]; then
  rm -f "$archive"
fi
if [[ ! -f "$archive" ]]; then
  # Progress goes to stderr so stdout stays a single clean line: the path to
  # the pinned interpreter, which callers capture with $(...).
  printf '==> downloading pinned portable Python %s\n' "$PYTHON_VERSION" >&2
  curl --proto '=https' --tlsv1.2 -L -C - --fail --retry 10 \
    --retry-all-errors --connect-timeout 30 --progress-bar \
    -o "$partial" "$URL"
  [[ "$(shasum -a 256 "$partial" | awk '{print $1}')" == \
      "$PYTHON_DARWIN_ARM64_SHA256" ]] || {
    rm -f "$partial"
    echo "portable Python checksum mismatch" >&2
    exit 1
  }
  mv -f "$partial" "$archive"
fi

destination="$ROOT/.tools/python-bootstrap"
python_bin="$destination/bin/python3"
valid=0
if [[ -x "$python_bin" ]] &&
   [[ "$("$python_bin" -c 'import platform; print(platform.python_version())')" == \
      "$PYTHON_VERSION" ]]; then
  valid=1
fi
if [[ "$valid" -ne 1 ]]; then
  stage="$(mktemp -d "$ROOT/.python-bootstrap-stage.XXXXXX")"
  trap 'rm -rf "${stage:-}"' EXIT
  tar -xzf "$archive" -C "$stage"
  [[ -x "$stage/python/bin/python3" ]] || {
    echo "portable Python archive has no python/bin/python3" >&2
    exit 1
  }
  mkdir -p "$ROOT/.tools"
  rm -rf "$destination"
  mv "$stage/python" "$destination"
  rm -rf "$stage"
  stage=""
  trap - EXIT
fi
[[ "$("$python_bin" -c 'import platform; print(platform.python_version())')" == \
    "$PYTHON_VERSION" ]] || {
  echo "portable Python runtime does not match $PYTHON_VERSION" >&2
  exit 1
}

"$python_bin" "$ROOT/verification/lifecycle.py" import-artifact \
  --root "$ROOT" --cache "$CACHE" \
  --artifact-id python-bootstrap-darwin-arm64 \
  --file "$archive" --url "$URL" \
  --requested-version "$PYTHON_VERSION" --resolved-version "$PYTHON_VERSION" \
  >/dev/null
printf '%s\n' "$python_bin"
