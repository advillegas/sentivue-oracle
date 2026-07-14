#!/usr/bin/env bash
# Wrap the verified .command installer in an unsigned macOS package. The
# package is the double-click Mac install: it publishes the immutable source,
# then continues the complete connected setup in a visible Terminal window.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION=""
INSTALLER=""
OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift ;;
    --installer) INSTALLER="${2:-}"; shift ;;
    --output) OUTPUT="${2:-}"; shift ;;
    *) echo "usage: build-macos-package.sh --version vX.Y.Z --installer FILE.command --output DIR" >&2; exit 2 ;;
  esac
  shift
done

[[ "$(uname -s)" == "Darwin" ]] || {
  echo "macOS package build requires a macOS host" >&2
  exit 2
}
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || {
  echo "invalid package version: $VERSION" >&2
  exit 2
}
[[ -f "$INSTALLER" && "$INSTALLER" == *.command ]] || {
  echo "verified .command installer is required" >&2
  exit 2
}
[[ -n "$OUTPUT" ]] || {
  echo "package output directory is required" >&2
  exit 2
}
command -v pkgbuild >/dev/null 2>&1 || {
  echo "pkgbuild is required" >&2
  exit 127
}
PYTHON_BIN="${ORACLE_PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 &&
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1 || {
  echo "Python 3.12 or newer is required to bind package provenance" >&2
  exit 127
}
BASE_PROVENANCE="$(dirname "$INSTALLER")/PROVENANCE.json"
BASE_CHECKSUMS="$(dirname "$INSTALLER")/SHA256SUMS"
[[ -f "$BASE_PROVENANCE" && -f "$BASE_CHECKSUMS" ]] || {
  echo "base installer provenance or checksums are missing" >&2
  exit 1
}
EXPECTED_INSTALLER_NAME="SentiVue-Oracle-Installer-$VERSION.command"
[[ "$(basename "$INSTALLER")" == "$EXPECTED_INSTALLER_NAME" ]] || {
  echo "installer name does not match requested version" >&2
  exit 1
}
VALIDATED="$("$PYTHON_BIN" - "$INSTALLER" "$BASE_PROVENANCE" "$BASE_CHECKSUMS" "$VERSION" "$ROOT" "$0" <<'PY'
import hashlib
import json
import pathlib
import re
import sys
import zipfile

installer, provenance_path, checksums_path = map(pathlib.Path, sys.argv[1:4])
version = sys.argv[4]
root = pathlib.Path(sys.argv[5]).resolve()
package_builder = pathlib.Path(sys.argv[6]).resolve()
provenance_bytes = provenance_path.read_bytes()
checksums_bytes = checksums_path.read_bytes()
provenance = json.loads(provenance_bytes.decode("utf-8"))
if provenance.get("version") != version:
    raise SystemExit("base provenance version mismatch")
revision = provenance.get("source_revision", "")
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("base provenance has no immutable source revision")
records = {
    item["name"]: item
    for item in provenance.get("artifacts", [])
    if isinstance(item, dict) and isinstance(item.get("name"), str)
}
record = records.get(installer.name)
actual = hashlib.sha256(installer.read_bytes()).hexdigest()
if (
    record is None
    or record.get("sha256") != actual
    or record.get("size") != installer.stat().st_size
):
    raise SystemExit("installer disagrees with base provenance")
checksums = {}
try:
    checksums_text = checksums_bytes.decode("ascii")
except UnicodeDecodeError:
    raise SystemExit("base checksum manifest is malformed")
if (
    not checksums_text
    or not checksums_text.endswith("\n")
    or "\r" in checksums_text
):
    raise SystemExit("base checksum manifest is malformed")
for line in checksums_text[:-1].split("\n"):
    match = re.fullmatch(
        r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)",
        line,
    )
    if match is None:
        raise SystemExit("base checksum manifest is malformed")
    digest, name = match.groups()
    if name in checksums:
        raise SystemExit("base checksum manifest is malformed")
    checksums[name] = digest
if checksums.get(installer.name) != actual:
    raise SystemExit("installer disagrees with base checksums")
provenance_sha = hashlib.sha256(provenance_bytes).hexdigest()
if checksums.get(provenance_path.name) != provenance_sha:
    raise SystemExit("base provenance is not checksummed")
builder = provenance.get("builder")
if (
    not isinstance(builder, dict)
    or builder.get("path") != "verification/lifecycle.py"
    or builder.get("immutable_source") is not True
    or not re.fullmatch(r"[0-9a-f]{64}", str(builder.get("sha256", "")))
):
    raise SystemExit("base builder provenance is invalid")
source_name = f"sentivue-oracle-{version}.zip"
source_record = records.get(source_name)
source_path = installer.parent / source_name
if (
    source_record is None
    or not source_path.is_file()
    or source_record.get("sha256") != hashlib.sha256(source_path.read_bytes()).hexdigest()
    or source_record.get("size") != source_path.stat().st_size
    or checksums.get(source_name) != source_record.get("sha256")
):
    raise SystemExit("canonical source archive is missing or invalid")
with zipfile.ZipFile(source_path) as archive:
    lifecycle_source = archive.read("sentivue-oracle/verification/lifecycle.py")
    package_builder_source = archive.read(
        "sentivue-oracle/bootstrap/build-macos-package.sh"
    )
if (
    hashlib.sha256(lifecycle_source).hexdigest() != builder["sha256"]
    or hashlib.sha256((root / "verification/lifecycle.py").read_bytes()).hexdigest()
    != builder["sha256"]
):
    raise SystemExit("checked-out lifecycle builder differs from immutable source")
package_builder_sha = hashlib.sha256(package_builder.read_bytes()).hexdigest()
if hashlib.sha256(package_builder_source).hexdigest() != package_builder_sha:
    raise SystemExit("checked-out package builder differs from immutable source")
dependency = provenance.get("dependency_cache")
dependency_name = "-"
dependency_sha = "-"
if dependency is not None:
    if (
        not isinstance(dependency, dict)
        or dependency.get("bundle_format") != "zip-sidecar"
        or not isinstance(dependency.get("bundle_name"), str)
        or not re.fullmatch(
            r"SentiVue-Oracle-Dependencies-v[0-9A-Za-z.-]+\.zip",
            dependency["bundle_name"],
        )
        or not re.fullmatch(r"[0-9a-f]{64}", str(dependency.get("bundle_sha256", "")))
    ):
        raise SystemExit("base dependency sidecar provenance is invalid")
    dependency_name = dependency["bundle_name"]
    dependency_path = installer.parent / dependency_name
    dependency_sha = hashlib.sha256(dependency_path.read_bytes()).hexdigest()
    if (
        dependency_sha != dependency["bundle_sha256"]
        or checksums.get(dependency_name) != dependency_sha
        or records.get(dependency_name, {}).get("sha256") != dependency_sha
    ):
        raise SystemExit("base dependency sidecar is missing or invalid")
print(
    "\t".join(
        (
            revision,
            actual,
            provenance_sha,
            hashlib.sha256(checksums_bytes).hexdigest(),
            builder["sha256"],
            package_builder_sha,
            dependency_name,
            dependency_sha,
        )
    )
)
PY
)" || {
  echo "base installer verification failed" >&2
  exit 1
}
IFS=$'\t' read -r SOURCE_REVISION INSTALLER_SHA BASE_PROVENANCE_SHA BASE_CHECKSUMS_SHA BASE_BUILDER_SHA IMMUTABLE_PACKAGE_BUILDER_SHA DEPENDENCY_BUNDLE_NAME DEPENDENCY_BUNDLE_SHA <<< "$VALIDATED"

mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sentivue-oracle-pkg.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT HUP INT TERM
SCRIPTS="$WORK/scripts"
mkdir "$SCRIPTS"
cp "$INSTALLER" "$SCRIPTS/one-click.command"
chmod 755 "$SCRIPTS/one-click.command"
printf '%s  one-click.command\n' "$INSTALLER_SHA" > "$SCRIPTS/one-click.command.sha256"
if [[ "$DEPENDENCY_BUNDLE_NAME" != "-" ]]; then
  cp "$(dirname "$INSTALLER")/$DEPENDENCY_BUNDLE_NAME" \
    "$SCRIPTS/$DEPENDENCY_BUNDLE_NAME"
  printf '%s  %s\n' "$DEPENDENCY_BUNDLE_SHA" "$DEPENDENCY_BUNDLE_NAME" \
    >> "$SCRIPTS/one-click.command.sha256"
fi
cat > "$SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_LOG="$(mktemp /tmp/sentivue-oracle-install.XXXXXX.log)"
exec > >(/usr/bin/tee -a "$INSTALL_LOG") 2>&1
report_failure() {
  # Installer.app only shows a generic failure dialog; leave the actual error
  # on the console user's Desktop so it is diagnosable without log spelunking.
  local status=$?
  [[ "$status" -ne 0 ]] || exit 0
  local console_user user_home report
  console_user="$(stat -f '%Su' /dev/console 2>/dev/null || true)"
  if [[ -n "$console_user" && "$console_user" != "root" ]]; then
    user_home="$(dscl . -read "/Users/$console_user" NFSHomeDirectory 2>/dev/null | cut -d' ' -f2-)"
    if [[ -n "$user_home" && -d "$user_home/Desktop" ]]; then
      report="$user_home/Desktop/SentiVue Oracle Install Error.txt"
      {
        echo "SentiVue Oracle installation failed."
        echo "Send this file to support, or rerun the installer after fixing the cause."
        echo "Time: $(date)"
        echo
        echo "--- last installer output ---"
        tail -n 100 "$INSTALL_LOG" 2>/dev/null
      } > "$report" || true
      chown "$console_user" "$report" 2>/dev/null || true
    fi
  fi
  exit "$status"
}
trap report_failure EXIT
(cd "$SCRIPT_DIR" && shasum -a 256 -c one-click.command.sha256) || {
  echo "embedded source installer digest mismatch" >&2
  exit 1
}
CONSOLE_USER="$(stat -f '%Su' /dev/console)"
[[ -n "$CONSOLE_USER" && "$CONSOLE_USER" != "root" ]] || {
  echo "SentiVue Oracle package requires an interactive console user" >&2
  exit 1
}
USER_HOME="$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory | cut -d' ' -f2-)"
[[ -n "$USER_HOME" && -d "$USER_HOME" ]] || {
  echo "could not resolve the console user's home directory" >&2
  exit 1
}
DEST="$USER_HOME/sentivue-oracle"
/usr/bin/sudo -u "$CONSOLE_USER" /usr/bin/env \
  HOME="$USER_HOME" \
  ORACLE_INSTALLER_DEST="$DEST" \
  ORACLE_INSTALLER_SKIP_SETUP=1 \
  ORACLE_INSTALLER_REQUIRE_IMMUTABLE=1 \
  /bin/bash "$SCRIPT_DIR/one-click.command"
echo "SentiVue Oracle source installed at $DEST"
# Installer.app deletes its temporary Scripts directory when this script
# returns, so setup continues from a copy staged inside the installed tree.
RESUME_INSTALLER="$DEST/.oracle-resume-installer.command"
RESUME_LAUNCHER="$DEST/Resume Install.command"
/usr/bin/install -m 755 -o "$CONSOLE_USER" \
  "$SCRIPT_DIR/one-click.command" "$RESUME_INSTALLER"
LAUNCHER_TMP="$(mktemp)"
printf '#!/bin/bash\nexport ORACLE_INSTALLER_DEST=%q\nexec /bin/bash %q\n' \
  "$DEST" "$RESUME_INSTALLER" > "$LAUNCHER_TMP"
/usr/bin/install -m 755 -o "$CONSOLE_USER" "$LAUNCHER_TMP" "$RESUME_LAUNCHER"
rm -f "$LAUNCHER_TMP"
if [[ "${ORACLE_INSTALLER_SKIP_SETUP:-0}" == "1" || -n "${GITHUB_ACTIONS:-}" ]]; then
  echo "Source-only package mode: full setup was skipped."
  echo "Finish later by double-clicking: $RESUME_LAUNCHER"
  exit 0
fi
CONSOLE_UID="$(/usr/bin/id -u "$CONSOLE_USER")"
if /bin/launchctl asuser "$CONSOLE_UID" /usr/bin/sudo -u "$CONSOLE_USER" \
  /usr/bin/open -a Terminal "$RESUME_LAUNCHER"; then
  echo "Setup is continuing in a Terminal window: it selects a hardware"
  echo "profile, downloads every checksum-bound dependency and model shard,"
  echo "installs the engines and local IDE, and finishes on its own."
else
  echo "Could not open Terminal automatically."
  echo "Finish setup by double-clicking: $RESUME_LAUNCHER"
fi
exit 0
POSTINSTALL
chmod 755 "$SCRIPTS/postinstall"

PACKAGE="$OUTPUT/SentiVue-Oracle-Installer-$VERSION.pkg"
[[ ! -e "$PACKAGE" ]] || {
  echo "refusing to overwrite existing package: $PACKAGE" >&2
  exit 1
}
pkgbuild \
  --nopayload \
  --scripts "$SCRIPTS" \
  --identifier "io.sentivue.oracle.installer" \
  --version "${VERSION#v}" \
  "$PACKAGE"

PACKAGE_SHA="$(shasum -a 256 "$PACKAGE" | awk '{print $1}')"
PACKAGE_BUILDER_SHA="$(shasum -a 256 "$0" | awk '{print $1}')"
[[ "$PACKAGE_BUILDER_SHA" == "$IMMUTABLE_PACKAGE_BUILDER_SHA" ]] || {
  echo "package builder changed after immutable validation" >&2
  exit 1
}
CHECKSUM="$PACKAGE.sha256"
PROVENANCE="$PACKAGE.provenance.json"
cat > "$PROVENANCE" <<EOF
{
  "schema_version": 1,
  "version": "$VERSION",
  "source_revision": "$SOURCE_REVISION",
  "artifact": "$(basename "$PACKAGE")",
  "sha256": "$PACKAGE_SHA",
  "embedded_installer": "$(basename "$INSTALLER")",
  "embedded_installer_sha256": "$INSTALLER_SHA",
  "base_provenance_sha256": "$BASE_PROVENANCE_SHA",
  "base_checksums_sha256": "$BASE_CHECKSUMS_SHA",
  "base_builder_sha256": "$BASE_BUILDER_SHA",
  "package_builder_sha256": "$PACKAGE_BUILDER_SHA",
  "dependency_bundle": "$DEPENDENCY_BUNDLE_NAME",
  "dependency_bundle_sha256": "$DEPENDENCY_BUNDLE_SHA",
  "code_signing": "unsigned",
  "notarization": "not-notarized",
  "scope": "double-click Mac install; publishes immutable source, then continues complete connected setup in Terminal"
}
EOF
PROVENANCE_SHA="$(shasum -a 256 "$PROVENANCE" | awk '{print $1}')"
printf '%s  %s\n%s  %s\n' \
  "$PACKAGE_SHA" "$(basename "$PACKAGE")" \
  "$PROVENANCE_SHA" "$(basename "$PROVENANCE")" > "$CHECKSUM"
echo "$PACKAGE"
