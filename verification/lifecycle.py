"""Reproducible lifecycle primitives for SentiVue Oracle.

This module deliberately uses only the Python standard library.  Release and
package inputs come from Git objects, never the mutable worktree.  Install and
uninstall operations are constrained by an ownership manifest, and every text
write uses an atomic UTF-8-without-BOM replacement.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
PRODUCT_PREFIX = "sentivue-oracle"
STATE_DIRECTORY = ".install-state"
STATE_FILE = "state.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(
    r"^v?[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
PORTABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INSTALLER_HARDENING_TRANSFORM = "protected-builder-atomic-utf8-reparse-v2"
DEPENDENCY_AUTHORITY_FILE = "verification/dependency-authorities.json"

HARD_EXCLUDED_PARTS = {
    ".agent",
    ".git",
    ".install-state",
    ".superpowers",
    ".tools",
    ".venv",
    ".worktrees",
    "__pycache__",
    "artifacts",
    "backups",
    "incoming",
    "logs",
    "memory",
    "models",
    "node_modules",
    "quarantine",
    "reports",
    "state",
    "toolchains",
}
HARD_EXCLUDED_ROOTS = {"data"}
DATABASE_SUFFIXES = {".db", ".duckdb", ".sqlite", ".sqlite3"}
GENERATED_SUFFIXES = {".log", ".pyc", ".pyo", ".tmp"}
CREDENTIAL_NAMES = {
    ".env",
    "id_ed25519",
    "id_rsa",
}
PURGE_ROOTS = (
    STATE_DIRECTORY,
    ".tools",
    "artifacts",
    "backups",
    "incoming",
    "logs",
    "models",
    "reports",
    "state",
    "connectors/supabase/volumes",
    "engines/opencode/xdg-data",
    "harness/agent-mcp/vendor",
    "harness/ecc/vendor",
    "harness/loop-engineering/vendor",
    "harness/skill-packs/vendor",
)


class LifecycleError(RuntimeError):
    """A fail-closed lifecycle validation error."""


@dataclass(frozen=True)
class GitEntry:
    path: str
    object_id: str
    mode: int
    data: bytes


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    cache_path: str
    source_url: str
    requested_version: str
    resolved_version: str
    sha256: str
    size: int
    recorded_at: str
    trust: str = "untrusted"

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.artifact_id,
            "cache_path": self.cache_path,
            "source_url": self.source_url,
            "requested_version": self.requested_version,
            "resolved_version": self.resolved_version,
            "sha256": self.sha256,
            "size": self.size,
            "recorded_at": self.recorded_at,
            "trust": self.trust,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactRecord":
        return cls(
            artifact_id=str(payload.get("id", "")),
            cache_path=str(payload.get("cache_path", "")),
            source_url=str(payload.get("source_url", "")),
            requested_version=str(payload.get("requested_version", "")),
            resolved_version=str(payload.get("resolved_version", "")),
            sha256=str(payload.get("sha256", "")),
            size=int(payload.get("size", -1)),
            recorded_at=str(payload.get("recorded_at", "")),
            trust=str(payload.get("trust", "untrusted")),
        )


@dataclass(frozen=True)
class ReleaseBundle:
    version: str
    revision: str
    output_dir: Path
    archives: list[Path]
    checksums: Path
    provenance: Path


@dataclass(frozen=True)
class UninstallEntry:
    scope: str
    path: str
    action: str
    reason: str = ""


@dataclass(frozen=True)
class UninstallPlan:
    applied: bool
    purge: bool
    entries: list[UninstallEntry]


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    text: bool = True,
    input_data: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        input=input_data,
        env=dict(env) if env is not None else None,
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    text: bool = True,
    input_data: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    completed = _run(
        argv,
        cwd=cwd,
        text=text,
        input_data=input_data,
        env=env,
    )
    if completed.returncode != 0:
        error = completed.stderr or completed.stdout or "command failed"
        if isinstance(error, bytes):
            error = error.decode("utf-8", errors="replace")
        raise LifecycleError(
            f"{' '.join(str(item) for item in argv)} failed: {str(error).strip()}"
        )
    return completed


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Atomically replace *path* with bytes, cleaning temporary files on error."""

    # Do not resolve the final component: os.replace must replace a hostile
    # symlink itself rather than following it to unrelated user data.
    path = Path(os.path.abspath(os.fspath(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def quote_command_argument(value: str, platform: str) -> str:
    """Quote one argv element for the shell used by a llama-swap command."""

    if not value or any(ord(character) < 32 for character in value):
        raise LifecycleError("command argument is empty or contains a control character")
    if platform == "posix":
        return shlex.quote(value)
    if platform == "windows":
        return subprocess.list2cmdline([value])
    raise LifecycleError(f"unsupported command quoting platform: {platform}")


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Write explicit UTF-8 without BOM using atomic replacement."""

    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def validate_relative_path(value: str) -> str:
    """Validate a portable, archive-safe relative POSIX path."""

    if not isinstance(value, str) or not value:
        raise LifecycleError("unsafe empty relative path")
    if "\\" in value:
        raise LifecycleError(f"unsafe path uses a backslash: {value!r}")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise LifecycleError(f"unsafe absolute path: {value!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LifecycleError(f"unsafe control character in path: {value!r}")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise LifecycleError(f"unsafe traversal path: {value!r}")
    normalized = path.as_posix()
    if normalized != value or normalized.startswith("../"):
        raise LifecycleError(f"unsafe non-canonical path: {value!r}")
    return normalized


def _validate_version(version: str) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise LifecycleError(
            "version must be a new semantic version such as v1.2.3"
        )
    return version


def _resolve_revision(root: Path, revision: str) -> str:
    completed = _checked(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=root
    )
    resolved = str(completed.stdout).strip()
    if not COMMIT_PATTERN.fullmatch(resolved):
        raise LifecycleError(f"Git did not resolve an immutable commit: {resolved!r}")
    return resolved


def _git_blob(root: Path, revision: str, relative: str) -> bytes:
    validate_relative_path(relative)
    completed = _checked(
        ["git", "show", f"{revision}:{relative}"], cwd=root, text=False
    )
    return bytes(completed.stdout)


def _git_timestamp(root: Path, revision: str) -> int:
    completed = _checked(["git", "show", "-s", "--format=%ct", revision], cwd=root)
    try:
        return int(str(completed.stdout).strip())
    except ValueError as exc:
        raise LifecycleError("Git commit timestamp is invalid") from exc


def _git_entries(root: Path, revision: str) -> list[GitEntry]:
    listed = _checked(
        ["git", "ls-tree", "-r", "-z", "--full-tree", revision],
        cwd=root,
        text=False,
    )
    entries: list[GitEntry] = []
    for record in bytes(listed.stdout).split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise LifecycleError("Git tree contains an unreadable record")
        mode_text, object_type, object_id = fields
        path = os.fsdecode(raw_path)
        validate_relative_path(path)
        if object_type != b"blob":
            raise LifecycleError(f"{path}: unsupported Git object type")
        if mode_text == b"120000":
            raise LifecycleError(f"{path}: symbolic links are not package inputs")
        blob = _checked(
            ["git", "cat-file", "blob", object_id.decode("ascii")],
            cwd=root,
            text=False,
        )
        entries.append(
            GitEntry(
                path=path,
                object_id=object_id.decode("ascii"),
                mode=int(mode_text, 8),
                data=bytes(blob.stdout),
            )
        )
    return entries


def _load_json_blob(root: Path, revision: str, relative: str) -> dict[str, Any]:
    try:
        payload = json.loads(_git_blob(root, revision, relative).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"{relative}: invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LifecycleError(f"{relative}: expected a JSON object")
    return payload


def _package_allowlist(policy: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    allowlist = policy.get("package_allowlist")
    if not isinstance(allowlist, dict):
        raise LifecycleError("verification policy has no package_allowlist")
    roots = allowlist.get("roots")
    files = allowlist.get("files")
    if not isinstance(roots, list) or not isinstance(files, list):
        raise LifecycleError("package_allowlist roots/files must be lists")
    if not all(isinstance(item, str) and item for item in [*roots, *files]):
        raise LifecycleError("package_allowlist contains an invalid path")
    return set(roots), set(files)


def _hard_excluded(relative: str) -> bool:
    path = PurePosixPath(relative)
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts.intersection(HARD_EXCLUDED_PARTS):
        return True
    if path.parts and path.parts[0].lower() in HARD_EXCLUDED_ROOTS:
        return True
    lowered_name = path.name.lower()
    if lowered_name in CREDENTIAL_NAMES:
        return True
    if lowered_name.startswith("credentials") and lowered_name.endswith(".json"):
        return True
    if lowered_name.endswith((".key", ".pem", ".p12", ".pfx")):
        return True
    if path.suffix.lower() in DATABASE_SUFFIXES | GENERATED_SUFFIXES:
        return True
    return False


def _filter_package_entries(
    entries: Iterable[GitEntry], policy: Mapping[str, Any]
) -> list[GitEntry]:
    allowed_roots, allowed_files = _package_allowlist(policy)
    selected: list[GitEntry] = []
    outside: list[str] = []
    for entry in entries:
        if _hard_excluded(entry.path):
            continue
        first = entry.path.split("/", 1)[0]
        if entry.path not in allowed_files and first not in allowed_roots:
            outside.append(entry.path)
            continue
        selected.append(entry)
    if outside:
        raise LifecycleError(
            "tracked source outside package allowlist: " + ", ".join(sorted(outside))
        )
    return sorted(selected, key=lambda entry: entry.path)


def _parse_versions_text(text: str) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    errors: list[str] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            errors.append(f"VERSIONS.lock:{line_number}: expected KEY=value")
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.split("#", 1)[0].strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            errors.append(f"VERSIONS.lock:{line_number}: invalid key {key!r}")
            continue
        if key in pins:
            errors.append(f"VERSIONS.lock:{line_number}: duplicate key {key}")
            continue
        if not value:
            errors.append(f"VERSIONS.lock:{line_number}: empty value for {key}")
            continue
        pins[key] = value
    return pins, errors


def _parse_models_text(text: str) -> tuple[list[dict[str, str]], list[str]]:
    models: list[dict[str, str]] = []
    errors: list[str] = []
    names: set[str] = set()
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = [field.strip() for field in raw.split("|")]
        if len(fields) != 7:
            errors.append(
                f"serving/models.manifest:{line_number}: expected 7 fields "
                "including immutable revision or dynamic"
            )
            continue
        name, repository, include, slot, context, flags, revision = fields
        if not name or name in names:
            errors.append(
                f"serving/models.manifest:{line_number}: empty or duplicate model"
            )
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            errors.append(
                f"serving/models.manifest:{line_number}: model name is not portable"
            )
            continue
        names.add(name)
        if not repository or not include:
            errors.append(
                f"serving/models.manifest:{line_number}: repository/include is empty"
            )
        if revision != "dynamic" and not COMMIT_PATTERN.fullmatch(revision):
            errors.append(
                f"serving/models.manifest:{line_number}: revision must be a "
                "40-character commit or dynamic"
            )
        models.append(
            {
                "name": name,
                "repository": repository,
                "include": include,
                "slot": slot,
                "context": context,
                "flags": flags,
                "revision": revision,
            }
        )
    return models, errors


def _declared_models(root: Path) -> dict[str, dict[str, str]]:
    try:
        models, errors = _parse_models_text(
            (root / "serving" / "models.manifest").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"model manifest is unreadable: {exc}") from exc
    if errors:
        raise LifecycleError("; ".join(errors))
    return {model["name"]: model for model in models}


def _model_authorities(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "serving" / "model-authorities.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"model authority policy is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise LifecycleError("model authority policy has an unsupported schema")
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        raise LifecycleError("model authority policy models must be an object")
    authorities: dict[str, dict[str, Any]] = {}
    for name, raw in raw_models.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
            or not isinstance(raw, dict)
        ):
            raise LifecycleError("model authority policy contains an invalid model entry")
        repository = raw.get("repository")
        revision = raw.get("revision")
        include = raw.get("include")
        files = raw.get("files")
        if (
            not isinstance(repository, str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*",
                repository,
            )
            or not isinstance(revision, str)
            or not COMMIT_PATTERN.fullmatch(revision)
            or not isinstance(include, str)
            or not include
            or not isinstance(files, list)
            or not files
        ):
            raise LifecycleError(f"model authority is incomplete: {name}")
        seen: set[str] = set()
        normalized_files: list[dict[str, Any]] = []
        for index, file_entry in enumerate(files):
            if not isinstance(file_entry, dict):
                raise LifecycleError(
                    f"model authority {name} file {index} is not an object"
                )
            relative = validate_relative_path(str(file_entry.get("path", "")))
            digest = str(file_entry.get("sha256", ""))
            size = file_entry.get("size")
            if (
                relative in seen
                or not relative.lower().endswith(".gguf")
                or not fnmatch.fnmatchcase(relative, include)
                or not SHA256_PATTERN.fullmatch(digest)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                raise LifecycleError(
                    f"model authority {name} file {index} is invalid"
                )
            seen.add(relative)
            normalized_files.append(
                {"path": relative, "sha256": digest, "size": size}
            )
        authorities[name] = {
            "repository": repository,
            "revision": revision,
            "include": include,
            "files": sorted(normalized_files, key=lambda item: item["path"]),
        }
    return authorities


def _model_authority_digest(authority: Mapping[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(dict(authority)))


def _declared_model(
    root: Path,
    model_name: str,
    repository: str,
    requested_revision: str,
    resolved_revision: str,
) -> dict[str, str]:
    declared = _declared_models(root).get(model_name)
    if declared is None:
        raise LifecycleError(f"model is not declared in models.manifest: {model_name}")
    if declared["repository"] != repository:
        raise LifecycleError("model repository differs from models.manifest")
    revision = declared["revision"]
    if requested_revision != revision:
        raise LifecycleError("requested model revision differs from models.manifest")
    if not COMMIT_PATTERN.fullmatch(resolved_revision):
        raise LifecycleError("resolved model revision must be a 40-character commit")
    if revision != "dynamic" and resolved_revision != revision:
        raise LifecycleError("resolved model revision differs from models.manifest")
    return declared


def _local_model_files(
    model_root: Path,
    include: str,
) -> list[tuple[str, Path]]:
    if not model_root.is_dir() or _is_reparse_point(model_root):
        raise LifecycleError(f"model directory is missing or unsafe: {model_root}")
    files: list[tuple[str, Path]] = []
    for path in sorted(model_root.rglob("*.gguf")):
        if _is_reparse_point(path) or not path.is_file():
            raise LifecycleError(f"model snapshot contains an unsafe file: {path}")
        relative = path.relative_to(model_root).as_posix()
        validate_relative_path(relative)
        if not fnmatch.fnmatchcase(relative, include):
            raise LifecycleError(
                f"model file is outside the declared include pattern: {relative}"
            )
        files.append((relative, path))
    if not files:
        raise LifecycleError(f"model snapshot has no GGUF files: {model_root.name}")
    return files


def _artifact_requirements(
    versions: Mapping[str, str],
    models: Sequence[Mapping[str, str]],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    dependency_inputs = policy.get("dependency_inputs", [])
    if isinstance(dependency_inputs, list):
        for item in dependency_inputs:
            if not isinstance(item, dict):
                continue
            artifact_id = item.get("id")
            version_key = item.get("version_key")
            if not isinstance(artifact_id, str) or not isinstance(version_key, str):
                continue
            requirements.append(
                {
                    "id": artifact_id,
                    "kind": item.get("kind", "dependency"),
                    "requested_version": versions.get(version_key, ""),
                    "version_key": version_key,
                }
            )
    for model in models:
        requirements.append(
            {
                "id": f"model:{model['name']}",
                "kind": "model",
                "repository": model["repository"],
                "include": model["include"],
                "requested_version": model["revision"],
            }
        )
    return sorted(requirements, key=lambda item: str(item["id"]))


DEPENDENCY_KINDS = {
    "container",
    "git",
    "ide",
    "ide-extension",
    "native",
    "npm",
    "python",
    "toolchain",
}
UNRESOLVED_VALUES = {"", "dynamic", "unresolved"}


def _source_identity(
    source: Mapping[str, Any],
    versions: Mapping[str, str],
    *,
    requested_version: str,
    resolved_version: str,
) -> str:
    literal = source.get("identity")
    key = source.get("identity_key")
    if isinstance(literal, str):
        identity = literal
    elif isinstance(key, str):
        identity = versions.get(key, "")
    else:
        return ""
    identity_digest_key = source.get("identity_digest_key")
    identity_digest = (
        versions.get(identity_digest_key, "")
        if isinstance(identity_digest_key, str)
        else ""
    )
    return (
        identity.replace("{version}", requested_version)
        .replace("{resolved}", resolved_version)
        .replace("{identity_digest}", identity_digest)
    )


def _authoritative_source_url(
    source: Mapping[str, Any],
    versions: Mapping[str, str],
    *,
    requested_version: str,
    resolved_version: str,
) -> str:
    identity = _source_identity(
        source,
        versions,
        requested_version=requested_version,
        resolved_version=resolved_version,
    )
    template = source.get("url", identity)
    if not isinstance(template, str):
        return ""
    return (
        template.replace("{identity}", identity)
        .replace("{version}", requested_version)
        .replace("{resolved}", resolved_version)
    )


def _trusted_digest(
    source: Mapping[str, Any], versions: Mapping[str, str]
) -> str:
    key = source.get("artifact_digest_key", source.get("digest_key"))
    if not isinstance(key, str):
        return ""
    value = versions.get(key, "").lower()
    if value.startswith("sha256:"):
        value = value.removeprefix("sha256:")
    return value


def _trusted_revision(
    source: Mapping[str, Any], versions: Mapping[str, str]
) -> str:
    key = source.get("revision_key")
    return versions.get(key, "") if isinstance(key, str) else ""


def _trusted_resolved_version(
    source: Mapping[str, Any],
    versions: Mapping[str, str],
    requested_version: str,
) -> str:
    key = source.get("resolved_version_key")
    if isinstance(key, str):
        return versions.get(key, "")
    return requested_version


def _dependency_policy_errors(
    versions: Mapping[str, str], policy: Mapping[str, Any]
) -> list[str]:
    errors: list[str] = []
    authority_manifest = policy.get(
        "dependency_authority_manifest",
        DEPENDENCY_AUTHORITY_FILE,
    )
    if authority_manifest != DEPENDENCY_AUTHORITY_FILE:
        errors.append(
            "policy dependency_authority_manifest must name the tracked "
            f"{DEPENDENCY_AUTHORITY_FILE}"
        )
    raw_inputs = policy.get("dependency_inputs", [])
    if not isinstance(raw_inputs, list):
        return ["policy dependency_inputs must be a list"]
    seen_ids: set[str] = set()
    declared_version_keys: set[str] = set()
    trust_roots = policy.get("bootstrap_trust_roots", [])
    if not isinstance(trust_roots, list):
        errors.append("policy bootstrap_trust_roots must be a list")
        trust_roots = []
    for index, raw_trust in enumerate(trust_roots):
        if not isinstance(raw_trust, dict):
            errors.append(f"bootstrap_trust_roots[{index}] must be an object")
            continue
        trust_key = raw_trust.get("version_key")
        if trust_key is None:
            continue
        if not isinstance(trust_key, str) or trust_key not in versions:
            errors.append(
                f"bootstrap_trust_roots[{index}] has a missing version key"
            )
            continue
        declared_version_keys.add(trust_key)
        if not _is_exact_pin(versions[trust_key]):
            errors.append(f"{trust_key} must be an exact bootstrap trust-root pin")
    for index, raw in enumerate(raw_inputs):
        if not isinstance(raw, dict):
            errors.append(f"dependency_inputs[{index}] must be an object")
            continue
        artifact_id = raw.get("id")
        kind = raw.get("kind")
        version_key = raw.get("version_key")
        allow_dynamic = raw.get("allow_dynamic", False)
        optional = raw.get("optional", False)
        platforms = raw.get("platforms", [])
        if (
            not isinstance(artifact_id, str)
            or not PORTABLE_ID_PATTERN.fullmatch(artifact_id)
            or kind not in DEPENDENCY_KINDS
            or not isinstance(version_key, str)
            or not isinstance(allow_dynamic, bool)
            or not isinstance(optional, bool)
            or not isinstance(platforms, list)
            or any(not isinstance(item, str) or not item for item in platforms)
        ):
            errors.append(f"dependency_inputs[{index}] has invalid fields")
            continue
        declared_version_keys.add(version_key)
        if artifact_id in seen_ids:
            errors.append(f"duplicate dependency id: {artifact_id}")
        seen_ids.add(artifact_id)
        value = versions.get(version_key)
        if value is None:
            errors.append(f"{artifact_id}: missing {version_key} in VERSIONS.lock")
        elif value in {"dynamic", "unresolved"} and not allow_dynamic:
            errors.append(f"{version_key} must be exact, not {value}")
        elif value not in {"dynamic", "unresolved"} and not _is_exact_pin(value):
            errors.append(f"{version_key} must be an exact pin")
        source = raw.get("source")
        if not isinstance(source, dict):
            errors.append(f"{artifact_id}: authoritative source policy is missing")
            continue
        for source_key_name in (
            "identity_key",
            "digest_key",
            "artifact_digest_key",
            "identity_digest_key",
            "revision_key",
            "resolved_version_key",
        ):
            source_key = source.get(source_key_name)
            if isinstance(source_key, str):
                declared_version_keys.add(source_key)
        identity = source.get("identity")
        identity_key = source.get("identity_key")
        if (not isinstance(identity, str) or not identity) and not isinstance(
            identity_key, str
        ):
            errors.append(f"{artifact_id}: authoritative source identity is missing")
        elif isinstance(identity_key, str) and identity_key not in versions:
            errors.append(
                f"{artifact_id}: source identity key {identity_key} is absent from VERSIONS.lock"
            )
        digest_key = source.get("artifact_digest_key", source.get("digest_key"))
        if not isinstance(digest_key, str):
            errors.append(f"{artifact_id}: trusted digest key is missing")
        elif digest_key not in versions:
            errors.append(
                f"{artifact_id}: trusted digest key {digest_key} is absent from VERSIONS.lock"
            )
        if kind == "container":
            identity_digest_key = source.get("identity_digest_key")
            if not isinstance(identity_digest_key, str):
                errors.append(
                    f"{artifact_id}: immutable container identity digest key is missing"
                )
            elif identity_digest_key not in versions:
                errors.append(
                    f"{artifact_id}: container identity digest key "
                    f"{identity_digest_key} is absent from VERSIONS.lock"
                )
        if kind == "git":
            revision_key = source.get("revision_key")
            if not isinstance(revision_key, str):
                errors.append(f"{artifact_id}: trusted revision key is missing")
            elif revision_key not in versions:
                errors.append(
                    f"{artifact_id}: trusted revision key {revision_key} is absent from VERSIONS.lock"
                )
        if value in {"dynamic", "unresolved"}:
            resolved_key = source.get("resolved_version_key")
            if not isinstance(resolved_key, str):
                errors.append(
                    f"{artifact_id}: dynamic input needs a trusted resolved-version key"
                )
            elif resolved_key not in versions:
                errors.append(
                    f"{artifact_id}: resolved-version key {resolved_key} is absent from VERSIONS.lock"
                )
    pin_suffixes = ("_VERSION", "_PIN", "_TAG", "_IMAGE", "_NPM")
    for key in sorted(versions):
        if (
            key.endswith(pin_suffixes) or key.startswith("MCP_")
        ) and key not in declared_version_keys:
            errors.append(
                f"{key}: version-bearing input is absent from dependency policy"
            )
    return errors


def _source_manifest_entries(
    entries: Sequence[GitEntry],
) -> list[dict[str, Any]]:
    return [
        {
            "path": entry.path,
            "mode": format(entry.mode, "o"),
            "sha256": _sha256_bytes(entry.data),
            "size": len(entry.data),
        }
        for entry in entries
    ]


def _prepare_archive_entries(
    root: Path, revision: str
) -> tuple[list[GitEntry], dict[str, Any], dict[str, Any]]:
    policy = _load_json_blob(root, revision, "verification/policy.json")
    selected = _filter_package_entries(_git_entries(root, revision), policy)
    versions_blob = next(
        (entry.data for entry in selected if entry.path == "VERSIONS.lock"), b""
    )
    models_blob = next(
        (
            entry.data
            for entry in selected
            if entry.path == "serving/models.manifest"
        ),
        b"",
    )
    versions, version_errors = _parse_versions_text(
        versions_blob.decode("utf-8", errors="strict")
    )
    models, model_errors = _parse_models_text(
        models_blob.decode("utf-8", errors="strict")
    )
    if version_errors or model_errors:
        raise LifecycleError("; ".join([*version_errors, *model_errors]))
    dependency_errors = _dependency_policy_errors(versions, policy)
    if dependency_errors:
        raise LifecycleError("; ".join(dependency_errors))
    authority_blob = next(
        (
            entry.data
            for entry in selected
            if entry.path == DEPENDENCY_AUTHORITY_FILE
        ),
        b"",
    )
    if authority_blob:
        try:
            authorities = _dependency_authorities_from_payload(
                json.loads(authority_blob.decode("utf-8"))
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            LifecycleError,
        ) as exc:
            raise LifecycleError(
                "immutable dependency authority manifest is invalid"
            ) from exc
        raw_by_id = {
            str(raw.get("id")): raw
            for raw in policy.get("dependency_inputs", [])
            if isinstance(raw, dict) and isinstance(raw.get("id"), str)
        }
        for artifact_id, authority in authorities.items():
            raw = raw_by_id.get(artifact_id)
            if raw is None:
                raise LifecycleError(
                    f"{artifact_id}: immutable authority has no policy entry"
                )
            _validate_dependency_authority_policy(
                versions, raw, authority
            )
    selected_by_path = {entry.path: entry for entry in selected}
    if "env/pyproject.toml" in selected_by_path:
        lock_entry = selected_by_path.get("env/uv.lock")
        if lock_entry is None:
            raise LifecycleError("immutable revision lacks env/uv.lock")
        try:
            uv_lock = tomllib.loads(lock_entry.data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise LifecycleError("immutable env/uv.lock is invalid") from exc
        if uv_lock.get("version") != 1:
            raise LifecycleError("immutable env/uv.lock has an unsupported schema")
    artifact_manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": revision,
        "requirements": _artifact_requirements(versions, models, policy),
        "note": (
            "External artifacts are not embedded. Use bootstrap/"
            "export-dependencies.ps1 or .sh and retain its hashed manifest."
        ),
    }
    source_provenance = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": revision,
        "files": _source_manifest_entries(selected),
    }
    protected_builder = selected_by_path.get("bootstrap/build-installers.ps1")
    if protected_builder is not None:
        source_provenance["protected_builder_transform"] = {
            "id": INSTALLER_HARDENING_TRANSFORM,
            "path": "bootstrap/build-installers.ps1",
            "source_sha256": _sha256_bytes(protected_builder.data),
            "required_after_protected_builder": True,
            "reason": (
                "The protected source remains byte-identical; release fails closed "
                "unless its installer output is hardened after generation."
            ),
        }
    generated = [
        GitEntry(
            "ARTIFACTS.json",
            "",
            0o100644,
            _json_bytes(artifact_manifest),
        ),
        GitEntry(
            "SOURCE-PROVENANCE.json",
            "",
            0o100644,
            _json_bytes(source_provenance),
        ),
    ]
    return [*selected, *generated], artifact_manifest, source_provenance


def _tar_mode(entry: GitEntry) -> int:
    return 0o755 if entry.mode & 0o111 else 0o644


def _write_tar_gz(
    path: Path, entries: Sequence[GitEntry], timestamp: int
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=timestamp) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for entry in entries:
                        info = tarfile.TarInfo(f"{PRODUCT_PREFIX}/{entry.path}")
                        info.size = len(entry.data)
                        info.mode = _tar_mode(entry)
                        info.mtime = timestamp
                        info.uid = 0
                        info.gid = 0
                        info.uname = "root"
                        info.gname = "root"
                        with tempfile.SpooledTemporaryFile() as source:
                            source.write(entry.data)
                            source.seek(0)
                            archive.addfile(info, source)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_zip(path: Path, entries: Sequence[GitEntry], timestamp: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    date_time = time.gmtime(max(timestamp, 315532800))[:6]
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for entry in entries:
                info = zipfile.ZipInfo(
                    f"{PRODUCT_PREFIX}/{entry.path}",
                    date_time=date_time,
                )
                info.create_system = 3
                info.external_attr = (_tar_mode(entry) | stat.S_IFREG) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, entry.data)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _artifact_payload(paths: Sequence[Path]) -> list[dict[str, Any]]:
    return [
        {
            "name": path.name,
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(paths, key=lambda item: item.name)
    ]


def _write_release_metadata(
    output_dir: Path,
    version: str,
    revision: str,
    artifacts: Sequence[Path],
    source_timestamp: int,
    build_transforms: Sequence[Mapping[str, Any]] = (),
) -> tuple[Path, Path]:
    provenance = output_dir / "PROVENANCE.json"
    checksums = output_dir / "SHA256SUMS"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "source_revision": revision,
        "created_at": (
            datetime.fromtimestamp(source_timestamp, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "artifacts": _artifact_payload(artifacts),
        "builder": "verification/lifecycle.py",
        "build_transforms": [dict(item) for item in build_transforms],
    }
    atomic_write_bytes(provenance, _json_bytes(payload))
    checksum_targets = sorted([*artifacts, provenance], key=lambda item: item.name)
    checksum_text = "".join(
        f"{_sha256_file(path)}  {path.name}\n" for path in checksum_targets
    )
    atomic_write_text(checksums, checksum_text)
    return checksums, provenance


def build_source_archives(
    root: Path,
    revision: str,
    output_dir: Path,
    version: str,
) -> ReleaseBundle:
    """Build deterministic source archives from an immutable Git commit."""

    root = root.resolve()
    output_dir = output_dir.resolve()
    _validate_version(version)
    resolved = _resolve_revision(root, revision)
    entries, _requirements, _provenance = _prepare_archive_entries(root, resolved)
    timestamp = _git_timestamp(root, resolved)
    output_dir.mkdir(parents=True, exist_ok=True)
    tar_path = output_dir / f"{PRODUCT_PREFIX}-{version}.tar.gz"
    zip_path = output_dir / f"{PRODUCT_PREFIX}-{version}.zip"
    for path in (tar_path, zip_path):
        if path.exists():
            raise LifecycleError(f"refusing to overwrite existing artifact: {path}")
    _write_tar_gz(tar_path, entries, timestamp)
    _write_zip(zip_path, entries, timestamp)
    artifacts = [tar_path, zip_path]
    checksums, provenance = _write_release_metadata(
        output_dir,
        version,
        resolved,
        artifacts,
        timestamp,
    )
    return ReleaseBundle(
        version=version,
        revision=resolved,
        output_dir=output_dir,
        archives=artifacts,
        checksums=checksums,
        provenance=provenance,
    )


def _read_checksum_manifest(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"{path.name}: unreadable checksum manifest") from exc
    for line_number, line in enumerate(lines, 1):
        if not line:
            continue
        digest, separator, relative = line.partition("  ")
        if not separator or not SHA256_PATTERN.fullmatch(digest):
            raise LifecycleError(
                f"{path.name}:{line_number}: invalid checksum entry"
            )
        validate_relative_path(relative)
        if "/" in relative or relative in entries:
            raise LifecycleError(
                f"{path.name}:{line_number}: duplicate or nested asset path"
            )
        entries[relative] = digest
    if not entries:
        raise LifecycleError(f"{path.name}: checksum manifest is empty")
    return entries


def _archive_source_names(path: Path) -> set[str]:
    names: set[str] = set()
    if path.name.endswith(".tar.gz"):
        try:
            with tarfile.open(path, "r:gz") as archive:
                members = archive.getmembers()
                for member in members:
                    if member.issym() or member.islnk():
                        raise LifecycleError(f"{path.name}: archive contains a link")
                    if not member.isfile():
                        continue
                    name = member.name
                    validate_relative_path(name)
                    names.add(name)
        except (tarfile.TarError, OSError) as exc:
            raise LifecycleError(f"{path.name}: invalid tar archive") from exc
    elif path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    validate_relative_path(name)
                    unix_type = (info.external_attr >> 16) & 0o170000
                    if unix_type == stat.S_IFLNK:
                        raise LifecycleError(f"{path.name}: archive contains a link")
                    names.add(name)
        except (zipfile.BadZipFile, OSError) as exc:
            raise LifecycleError(f"{path.name}: invalid zip archive") from exc
    for name in names:
        prefix = f"{PRODUCT_PREFIX}/"
        if not name.startswith(prefix):
            raise LifecycleError(f"{path.name}: member is outside product prefix")
        relative = name[len(prefix) :]
        if _hard_excluded(relative):
            raise LifecycleError(f"{path.name}: prohibited member {relative}")
    return names


def _smoke_installer(path: Path) -> None:
    if path.suffix.lower() == ".command":
        data = path.read_bytes()
        if INSTALLER_HARDENING_TRANSFORM.encode("ascii") not in data:
            raise LifecycleError(f"{path.name}: required build transform is missing")
        if b"bash install || true" in data:
            raise LifecycleError(f"{path.name}: install failures are still suppressed")
        marker = b"\n__PAYLOAD_BELOW__\n"
        position = data.find(marker)
        if position < 0:
            raise LifecycleError(f"{path.name}: installer payload marker is missing")
        payload = data[position + len(marker) :]
        try:
            with tempfile.SpooledTemporaryFile() as temporary:
                temporary.write(payload)
                temporary.seek(0)
                with tarfile.open(fileobj=temporary, mode="r:gz") as archive:
                    names = [item.name for item in archive if item.isfile()]
        except tarfile.TarError as exc:
            raise LifecycleError(f"{path.name}: embedded tar is invalid") from exc
        if f"{PRODUCT_PREFIX}/ARTIFACTS.json" not in names:
            raise LifecycleError(f"{path.name}: embedded artifact manifest is missing")
    elif path.suffix.lower() == ".cmd":
        data = path.read_bytes()
        if INSTALLER_HARDENING_TRANSFORM.encode("ascii") not in data:
            raise LifecycleError(f"{path.name}: required build transform is missing")
        if b"Set-Content -Path" in data or b"Write-Utf8NoBomAtomic" not in data:
            raise LifecycleError(f"{path.name}: config writes are not hardened")
        marker = b"#==B64PAYLOAD==#"
        position = data.find(marker)
        if position < 0:
            raise LifecycleError(f"{path.name}: installer payload marker is missing")
        encoded = re.sub(rb"[^A-Za-z0-9+/=]", b"", data[position + len(marker) :])
        try:
            payload = base64.b64decode(encoded, validate=True)
            with tempfile.SpooledTemporaryFile() as temporary:
                temporary.write(payload)
                temporary.seek(0)
                with zipfile.ZipFile(temporary) as archive:
                    names = archive.namelist()
        except (binascii.Error, zipfile.BadZipFile) as exc:
            raise LifecycleError(f"{path.name}: embedded zip is invalid") from exc
        if f"{PRODUCT_PREFIX}/ARTIFACTS.json" not in names:
            raise LifecycleError(f"{path.name}: embedded artifact manifest is missing")


def verify_release_bundle(output_dir: Path) -> ReleaseBundle:
    """Validate every artifact, checksum, provenance record, and archive payload."""

    output_dir = output_dir.resolve()
    checksums_path = output_dir / "SHA256SUMS"
    provenance_path = output_dir / "PROVENANCE.json"
    if not checksums_path.is_file() or not provenance_path.is_file():
        raise LifecycleError("release bundle lacks SHA256SUMS or PROVENANCE.json")
    expected = _read_checksum_manifest(checksums_path)
    for name, digest in expected.items():
        path = output_dir / name
        if not path.is_file():
            raise LifecycleError(f"checksum target is missing: {name}")
        if _sha256_file(path) != digest:
            raise LifecycleError(f"checksum mismatch: {name}")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("PROVENANCE.json is invalid") from exc
    if not isinstance(provenance, dict) or provenance.get("schema_version") != 1:
        raise LifecycleError("PROVENANCE.json has an unsupported schema")
    revision = str(provenance.get("source_revision", ""))
    version = str(provenance.get("version", ""))
    if not COMMIT_PATTERN.fullmatch(revision):
        raise LifecycleError("PROVENANCE.json lacks an immutable source revision")
    _validate_version(version)
    records = provenance.get("artifacts")
    if not isinstance(records, list) or not records:
        raise LifecycleError("PROVENANCE.json has no artifact records")
    assets: list[Path] = []
    for payload in records:
        if not isinstance(payload, dict):
            raise LifecycleError("PROVENANCE.json has a malformed artifact")
        name = validate_relative_path(str(payload.get("name", "")))
        if "/" in name:
            raise LifecycleError("release artifacts must be direct children")
        path = output_dir / name
        digest = str(payload.get("sha256", ""))
        size = payload.get("size")
        if not path.is_file():
            raise LifecycleError(f"provenance target is missing: {name}")
        if digest != _sha256_file(path) or size != path.stat().st_size:
            raise LifecycleError(f"provenance mismatch: {name}")
        if expected.get(name) != digest:
            raise LifecycleError(f"checksum/provenance disagreement: {name}")
        assets.append(path)

    installers = [
        path for path in assets if path.suffix.lower() in {".command", ".cmd"}
    ]
    transforms = provenance.get("build_transforms")
    if installers:
        if not isinstance(transforms, list):
            raise LifecycleError("PROVENANCE.json lacks installer build transforms")
        transformed_names = {
            str(item.get("artifact"))
            for item in transforms
            if isinstance(item, dict)
            and item.get("id") == INSTALLER_HARDENING_TRANSFORM
            and SHA256_PATTERN.fullmatch(str(item.get("source_sha256", "")))
        }
        if transformed_names != {path.name for path in installers}:
            raise LifecycleError(
                "PROVENANCE.json installer transform records are incomplete"
            )

    source_archives = [
        path
        for path in assets
        if path.name.endswith(".tar.gz") or path.suffix.lower() == ".zip"
    ]
    archive_sets = [
        _archive_source_names(path)
        for path in source_archives
        if path.name.startswith(f"{PRODUCT_PREFIX}-")
    ]
    if archive_sets:
        required = {
            f"{PRODUCT_PREFIX}/ARTIFACTS.json",
            f"{PRODUCT_PREFIX}/SOURCE-PROVENANCE.json",
        }
        for names in archive_sets:
            if not required <= names:
                raise LifecycleError("source archive lacks generated manifests")
        if any(names != archive_sets[0] for names in archive_sets[1:]):
            raise LifecycleError("source archives contain different file sets")
    for path in assets:
        if path.suffix.lower() in {".command", ".cmd"}:
            _smoke_installer(path)
    if expected.get("PROVENANCE.json") != _sha256_file(provenance_path):
        raise LifecycleError("PROVENANCE.json is not checksummed")
    return ReleaseBundle(
        version=version,
        revision=revision,
        output_dir=output_dir,
        archives=assets,
        checksums=checksums_path,
        provenance=provenance_path,
    )


def _materialize_entries(destination: Path, entries: Sequence[GitEntry]) -> None:
    for entry in entries:
        relative = validate_relative_path(entry.path)
        target = destination / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
        try:
            target.chmod(_tar_mode(entry))
        except OSError:
            pass


def _find_powershell() -> str | None:
    for candidate in ("powershell", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    fixed = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return str(fixed) if fixed.is_file() else None


def _installer_atomic_helper() -> str:
    """Return the ASCII PowerShell helper injected into protected installers."""

    return r'''
# ORACLE_BUILD_TRANSFORM=protected-builder-atomic-utf8-reparse-v2
function Assert-SafeAtomicPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (Test-Path -LiteralPath $full) {
        $target = Get-Item -LiteralPath $full -Force
        if (($target.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "atomic write target is a reparse point: $full"
        }
    }
    $cursor = Split-Path -Parent $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "atomic write ancestor is a reparse point: $cursor"
            }
        }
        $next = Split-Path -Parent $cursor
        if (-not $next -or $next -eq $cursor) { break }
        $cursor = $next
    }
}
function Write-Utf8NoBomAtomic([string]$Path, [string]$Text) {
    Assert-SafeAtomicPath -Path $Path
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Assert-SafeAtomicPath -Path $Path
    $temporary = Join-Path $parent (".{0}.{1}.tmp" -f [IO.Path]::GetFileName($Path), [Guid]::NewGuid().ToString("N"))
    try {
        [IO.File]::WriteAllText($temporary, $Text, (New-Object Text.UTF8Encoding($false)))
        Assert-SafeAtomicPath -Path $Path
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}
'''.strip("\n")


def _harden_built_installer(path: Path) -> dict[str, Any]:
    """Apply the required post-build transform without changing the protected source."""

    data = path.read_bytes()
    marker_line = f"ORACLE_BUILD_TRANSFORM={INSTALLER_HARDENING_TRANSFORM}"
    if path.suffix.lower() == ".command":
        payload_marker = b"\n__PAYLOAD_BELOW__\n"
        if data.count(payload_marker) != 1:
            raise LifecycleError("protected installer output lacks one macOS payload marker")
        header, payload = data.split(payload_marker, 1)
        try:
            text = header.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LifecycleError("protected installer header is not UTF-8") from exc
        if text.count("bash install || true") != 1:
            raise LifecycleError(
                "protected installer macOS failure-suppression pattern changed"
            )
        text = text.replace("bash install || true", "bash install")
        if not text.startswith("#!/bin/bash\n"):
            raise LifecycleError("protected installer macOS shebang changed")
        text = text.replace(
            "#!/bin/bash\n",
            f"#!/bin/bash\n# {marker_line}\n",
            1,
        )
        atomic_write_bytes(
            path, text.encode("utf-8") + payload_marker + payload, mode=0o755
        )
        return {
            "id": INSTALLER_HARDENING_TRANSFORM,
            "artifact": path.name,
            "changes": ["propagate-install-failure", "utf8-header"],
        }
    if path.suffix.lower() != ".cmd":
        raise LifecycleError(f"protected installer has an unexpected format: {path.name}")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise LifecycleError("protected installer Windows output is not ASCII") from exc
    if text.count("#==B64PAYLOAD==#") != 1:
        raise LifecycleError("protected installer output lacks one Windows payload marker")
    if text.count('$ErrorActionPreference = "Stop"') != 1:
        raise LifecycleError("protected installer PowerShell preamble changed")
    if text.count("Set-Content -Path") != 2:
        raise LifecycleError("protected installer config-write patterns changed")
    helper = _installer_atomic_helper()
    text = text.replace(
        '$ErrorActionPreference = "Stop"',
        '$ErrorActionPreference = "Stop"\n' + helper,
        1,
    )
    profile_write = (
        'Set-Content -Path (Join-Path $dest "serving\\models.profile") '
        '-Value (($sel.Models -split ",") -join "`n")'
    )
    tiers_write = (
        'Set-Content -Path (Join-Path $dest "serving\\tiers.env") '
        '-Value @("OPUS_MODEL=$($sel.Opus)", "SONNET_MODEL=$($sel.Sonnet)", '
        '"HAIKU_MODEL=$($sel.Haiku)")'
    )
    if text.count(profile_write) != 1 or text.count(tiers_write) != 1:
        raise LifecycleError("protected installer exact config-write patterns changed")
    text = text.replace(
        profile_write,
        (
            'Write-Utf8NoBomAtomic (Join-Path $dest "serving\\models.profile") '
            '((($sel.Models -split ",") -join "`n") + "`n")'
        ),
        1,
    )
    text = text.replace(
        tiers_write,
        (
            'Write-Utf8NoBomAtomic (Join-Path $dest "serving\\tiers.env") '
            '((@("OPUS_MODEL=$($sel.Opus)", "SONNET_MODEL=$($sel.Sonnet)", '
            '"HAIKU_MODEL=$($sel.Haiku)") -join "`n") + "`n")'
        ),
        1,
    )
    model_acquisition = (
        '& powershell -NoProfile -ExecutionPolicy Bypass -File '
        '(Join-Path $dest "bootstrap\\download-models.ps1")'
    )
    if text.count(model_acquisition) != 1:
        raise LifecycleError("protected installer model-acquisition pattern changed")
    text = text.replace(
        model_acquisition,
        (
            'throw "Model acquisition is a separate explicit operation; '
            'import a policy-bound cache before offline installation."'
        ),
        1,
    )
    checked_commands = (
        (
            '& powershell -NoProfile -ExecutionPolicy Bypass -File '
            '(Join-Path $dest "connectors\\ide\\setup-ide.ps1") install'
        ),
        (
            '& powershell -NoProfile -ExecutionPolicy Bypass -File '
            '(Join-Path $dest "bin\\oracle.ps1") setup'
        ),
    )
    for command in checked_commands:
        if text.count(command) != 1:
            raise LifecycleError("protected installer child-command pattern changed")
        text = text.replace(
            command,
            command
            + '\n        if ($LASTEXITCODE -ne 0) { throw "installer child command failed: $LASTEXITCODE" }',
            1,
        )
    if "Set-Content -Path" in text:
        raise LifecycleError("protected installer still has ambiguous config writes")
    atomic_write_bytes(path, text.encode("ascii"))
    return {
        "id": INSTALLER_HARDENING_TRANSFORM,
        "artifact": path.name,
        "changes": [
            "atomic-utf8-no-bom-config",
            "reject-reparse-config-paths",
            "separate-online-acquisition",
            "propagate-child-failures",
        ],
    }


def _build_installers(
    root: Path,
    revision: str,
    output_dir: Path,
    version: str,
) -> tuple[list[Path], list[dict[str, Any]]]:
    executable = _find_powershell()
    if not executable:
        raise LifecycleError("PowerShell is required to build both installer formats")
    entries, _requirements, source_provenance = _prepare_archive_entries(
        root, revision
    )
    transform_disclosure = source_provenance.get("protected_builder_transform")
    if not isinstance(transform_disclosure, dict):
        raise LifecycleError(
            "immutable source does not disclose the protected installer transform"
        )
    source_timestamp = _git_timestamp(root, revision)
    release_environment = dict(os.environ)
    release_environment.update(
        {
            "SOURCE_DATE_EPOCH": str(source_timestamp),
            "TZ": "UTC",
            "LC_ALL": "C",
        }
    )
    commit_environment = dict(release_environment)
    commit_environment.update(
        {
            "GIT_AUTHOR_DATE": f"@{source_timestamp} +0000",
            "GIT_COMMITTER_DATE": f"@{source_timestamp} +0000",
        }
    )
    with tempfile.TemporaryDirectory(prefix="oracle immutable release ") as temporary:
        stage = Path(temporary) / "source with spaces"
        stage.mkdir()
        _materialize_entries(stage, entries)
        _checked(["git", "init", "-q", str(stage)], cwd=stage)
        _checked(["git", "-c", "core.autocrlf=false", "add", "-A"], cwd=stage)
        _checked(
            [
                "git",
                "-c",
                "user.name=Oracle Release",
                "-c",
                "user.email=release@localhost",
                "commit",
                "-q",
                "-m",
                "immutable release input",
            ],
            cwd=stage,
            env=commit_environment,
        )
        _checked(["git", "tag", version], cwd=stage)
        builder = stage / "bootstrap" / "build-installers.ps1"
        if not builder.is_file():
            raise LifecycleError("immutable revision lacks build-installers.ps1")
        built = Path(temporary) / "installer output with spaces"
        built.mkdir()
        _checked(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(builder),
                "-Version",
                version,
                "-OutDir",
                str(built),
            ],
            cwd=stage,
            env=release_environment,
        )
        names = (
            f"SentiVue-Oracle-Installer-{version}.command",
            f"SentiVue-Oracle-Setup-{version}.cmd",
        )
        results: list[Path] = []
        transforms: list[dict[str, Any]] = []
        for name in names:
            source = built / name
            if not source.is_file():
                raise LifecycleError(f"installer builder did not produce {name}")
            target = output_dir / name
            if target.exists():
                raise LifecycleError(f"refusing to overwrite existing artifact: {target}")
            shutil.copyfile(source, target)
            transform = _harden_built_installer(target)
            transform["source_path"] = transform_disclosure["path"]
            transform["source_sha256"] = transform_disclosure["source_sha256"]
            transforms.append(transform)
            results.append(target)
        return results, transforms


def _require_release_inputs_match_revision(root: Path, revision: str) -> None:
    for relative in (
        "VERSIONS.lock",
        "bootstrap/build-installers.ps1",
        "verification/policy.json",
        DEPENDENCY_AUTHORITY_FILE,
        "serving/models.manifest",
        "serving/model-authorities.json",
        "env/pyproject.toml",
        "env/uv.lock",
    ):
        path = root / relative
        committed = _git_blob(root, revision, relative)
        try:
            working = path.read_bytes()
        except OSError as exc:
            raise LifecycleError(f"release authority input is unreadable: {relative}") from exc
        if working != committed:
            raise LifecycleError(
                f"release authority input differs from immutable revision: {relative}"
            )


def _build_release_bundle(
    root: Path,
    version: str,
    output_dir: Path,
    revision: str,
) -> ReleaseBundle:
    source_bundle = build_source_archives(root, revision, output_dir, version)
    installers, transforms = _build_installers(
        root.resolve(),
        source_bundle.revision,
        source_bundle.output_dir,
        version,
    )
    artifacts = [*source_bundle.archives, *installers]
    checksums, provenance = _write_release_metadata(
        source_bundle.output_dir,
        version,
        source_bundle.revision,
        artifacts,
        _git_timestamp(root, source_bundle.revision),
        build_transforms=transforms,
    )
    bundle = ReleaseBundle(
        version=version,
        revision=source_bundle.revision,
        output_dir=source_bundle.output_dir,
        archives=artifacts,
        checksums=checksums,
        provenance=provenance,
    )
    verified = verify_release_bundle(bundle.output_dir)
    if verified.revision != bundle.revision or verified.version != bundle.version:
        raise LifecycleError("release preflight provenance changed unexpectedly")
    return bundle


def _release_paths(bundle: ReleaseBundle) -> list[Path]:
    return [*bundle.archives, bundle.checksums, bundle.provenance]


def _files_equal(first: Path, second: Path) -> bool:
    with first.open("rb") as first_handle, second.open("rb") as second_handle:
        while True:
            first_chunk = first_handle.read(1024 * 1024)
            second_chunk = second_handle.read(1024 * 1024)
            if first_chunk != second_chunk:
                return False
            if not first_chunk:
                return True


def _reuse_immutable_release_output(
    expected: ReleaseBundle,
    output_dir: Path,
) -> ReleaseBundle:
    """Compare every existing byte to a fresh rebuild, then create only missing files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    expected_paths = {path.name: path for path in _release_paths(expected)}
    for existing in output_dir.iterdir():
        if not existing.is_file() or existing.name not in expected_paths:
            raise LifecycleError(
                f"existing release output differs from immutable rebuild: {existing.name}"
            )
        authoritative = expected_paths[existing.name]
        if (
            existing.stat().st_size != authoritative.stat().st_size
            or _sha256_file(existing) != _sha256_file(authoritative)
            or not _files_equal(existing, authoritative)
        ):
            raise LifecycleError(
                f"existing release output differs from immutable rebuild: {existing.name}"
            )
    for name, authoritative in sorted(expected_paths.items()):
        destination = output_dir / name
        if destination.exists():
            continue
        try:
            with destination.open("xb") as output:
                with authoritative.open("rb") as source:
                    shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError as exc:
            raise LifecycleError(
                f"release output appeared during create-only resume: {name}"
            ) from exc
    verified = verify_release_bundle(output_dir)
    if (
        verified.version != expected.version
        or verified.revision != expected.revision
    ):
        raise LifecycleError("resumed release output differs from immutable rebuild")
    return verified


def preflight_release(
    root: Path,
    version: str,
    output_dir: Path,
    revision: str = "HEAD",
) -> ReleaseBundle:
    """Build and smoke-validate all local assets before publication is possible."""

    root = root.resolve()
    resolved_revision = _resolve_revision(root, revision)
    current_revision = _current_revision(root)
    if resolved_revision != current_revision:
        raise LifecycleError(
            "release dependency policy must be validated from the checked-out revision"
        )
    _require_release_inputs_match_revision(root, resolved_revision)
    dependency_cache = root / "incoming" / "dependency-cache"
    dependency_manifest = dependency_cache / "manifest.json"
    dependency_errors = validate_dependency_inputs(
        root,
        artifact_manifest=(
            dependency_manifest if dependency_manifest.is_file() else None
        ),
        cache_root=dependency_cache,
        reproducible=True,
    )
    if dependency_errors:
        raise LifecycleError(
            "release requires policy-bound reproducible dependencies: "
            + "; ".join(dependency_errors)
        )
    output_dir = output_dir.resolve()
    if output_dir.is_dir() and any(output_dir.iterdir()):
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".release-rebuild-", dir=output_dir.parent
        ) as temporary_directory:
            expected = _build_release_bundle(
                root,
                version,
                Path(temporary_directory),
                resolved_revision,
            )
            return _reuse_immutable_release_output(expected, output_dir)
    return _build_release_bundle(root, version, output_dir, resolved_revision)


def _existing_tag_targets(
    root: Path, version: str
) -> tuple[str | None, str | None]:
    local_result = _run(
        ["git", "rev-parse", f"refs/tags/{version}^{{commit}}"], cwd=root
    )
    local = str(local_result.stdout).strip() if local_result.returncode == 0 else None
    if local is not None and not COMMIT_PATTERN.fullmatch(local):
        raise LifecycleError(f"local release tag is malformed: {version}")
    remote_result = _run(
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--tags",
            "origin",
            f"refs/tags/{version}",
        ],
        cwd=root,
    )
    remote: str | None = None
    if remote_result.returncode == 0:
        first_line = str(remote_result.stdout).splitlines()[0] if remote_result.stdout else ""
        remote = first_line.split(maxsplit=1)[0]
        if not COMMIT_PATTERN.fullmatch(remote):
            raise LifecycleError(f"remote release tag is malformed: {version}")
    return local, remote


def _existing_release_assets(
    root: Path, version: str
) -> dict[str, str] | None:
    result = _run(
        ["gh", "release", "view", version, "--json", "tagName,assets"],
        cwd=root,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(str(result.stdout))
    except json.JSONDecodeError as exc:
        raise LifecycleError("existing release metadata is invalid") from exc
    if not isinstance(payload, dict) or payload.get("tagName") != version:
        raise LifecycleError("existing release is not bound to the requested tag")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise LifecycleError("existing release asset metadata is invalid")
    assets: dict[str, str] = {}
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise LifecycleError("existing release has a malformed asset")
        name = validate_relative_path(str(raw.get("name", "")))
        if "/" in name or name in assets:
            raise LifecycleError("existing release has an unsafe or duplicate asset")
        assets[name] = str(raw.get("digest", ""))
    return assets


def publish_release(
    root: Path,
    version: str,
    output_dir: Path,
    revision: str = "HEAD",
) -> ReleaseBundle:
    """Create a new tag and release only after a complete local preflight."""

    root = root.resolve()
    _validate_version(version)
    bundle = preflight_release(root, version, output_dir, revision)
    verify_release_bundle(bundle.output_dir)
    local_tag, remote_tag = _existing_tag_targets(root, version)
    for location, target in (("local", local_tag), ("remote", remote_tag)):
        if target is not None and target != bundle.revision:
            raise LifecycleError(
                f"{location} release tag {version} points to a different revision"
            )
    release_assets = _existing_release_assets(root, version)
    if release_assets is not None and remote_tag is None:
        raise LifecycleError("existing release has no matching remote tag")
    release_paths = [
        *bundle.archives,
        bundle.checksums,
        bundle.provenance,
    ]
    expected_assets = {path.name: _sha256_file(path) for path in release_paths}
    missing_assets: list[Path] = []
    if release_assets is not None:
        unexpected = set(release_assets) - set(expected_assets)
        if unexpected:
            raise LifecycleError(
                "existing release has unexpected immutable assets: "
                + ", ".join(sorted(unexpected))
            )
        for path in release_paths:
            digest = release_assets.get(path.name)
            if digest is None:
                missing_assets.append(path)
            elif digest != f"sha256:{expected_assets[path.name]}":
                raise LifecycleError(
                    f"existing release asset digest differs: {path.name}"
                )

    commands: list[list[str]] = []
    if local_tag is None:
        commands.append(["git", "tag", version, bundle.revision])
    if remote_tag is None:
        commands.append(["git", "push", "origin", f"refs/tags/{version}"])
    if release_assets is None:
        commands.append(
            [
            "gh",
            "release",
            "create",
            version,
            *[str(path) for path in release_paths],
            "--title",
            f"SentiVue Oracle {version}",
            "--notes",
            (
                f"Immutable release from {bundle.revision}. "
                "Verify every asset with SHA256SUMS."
            ),
            ]
        )
    elif missing_assets:
        commands.append(
            ["gh", "release", "upload", version, *[str(path) for path in missing_assets]]
        )
    for command in commands:
        completed = _run(command, cwd=root)
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "command failed"
            raise LifecycleError(
                f"release stopped after {' '.join(command[:2])}: {detail.strip()}"
            )
    return bundle


def _is_exact_pin(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered or lowered in {
        "dynamic",
        "head",
        "latest",
        "main",
        "master",
        "unresolved",
    }:
        return False
    if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?"
        r"==[A-Za-z0-9][A-Za-z0-9._+-]*",
        value,
    ):
        return True
    if any(character in value for character in "*<>=^~, \t"):
        return False
    return True


def _load_artifact_records(
    manifest_path: Path | None,
) -> tuple[dict[str, ArtifactRecord], list[str]]:
    if manifest_path is None:
        return {}, []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"{manifest_path}: invalid artifact manifest: {exc}"]
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}, [f"{manifest_path}: unsupported artifact manifest schema"]
    raw_records = payload.get("artifacts")
    if not isinstance(raw_records, list):
        return {}, [f"{manifest_path}: artifacts must be a list"]
    records: dict[str, ArtifactRecord] = {}
    errors: list[str] = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            errors.append(f"artifact manifest entry {index} is not an object")
            continue
        try:
            record = ArtifactRecord.from_payload(raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"artifact manifest entry {index}: {exc}")
            continue
        if record.artifact_id in records:
            errors.append(f"duplicate artifact id: {record.artifact_id}")
        records[record.artifact_id] = record
    return records, errors


def validate_artifact_manifest(
    manifest_path: Path, cache_root: Path | None = None
) -> list[str]:
    cache_root = (cache_root or manifest_path.parent).resolve()
    records, errors = _load_artifact_records(manifest_path)
    for artifact_id, record in records.items():
        if not PORTABLE_ID_PATTERN.fullmatch(artifact_id):
            errors.append(f"{artifact_id!r}: invalid artifact id")
        try:
            relative = validate_relative_path(record.cache_path)
        except LifecycleError as exc:
            errors.append(f"{artifact_id}: {exc}")
            continue
        if not SHA256_PATTERN.fullmatch(record.sha256):
            errors.append(f"{artifact_id}: invalid SHA-256")
        if record.size < 0:
            errors.append(f"{artifact_id}: invalid size")
        if record.trust not in {"policy-bound", "untrusted"}:
            errors.append(f"{artifact_id}: invalid trust classification")
        if not _is_exact_pin(record.resolved_version):
            errors.append(f"{artifact_id}: resolved version is not exact")
        path = cache_root / Path(*PurePosixPath(relative).parts)
        if not path.is_file():
            errors.append(f"{artifact_id}: cached file is missing")
            continue
        if path.stat().st_size != record.size:
            errors.append(f"{artifact_id}: cached size mismatch")
        if _sha256_file(path) != record.sha256:
            errors.append(f"{artifact_id}: checksum mismatch")
    return errors


def resolve_cached_artifact(
    manifest_path: Path,
    cache_root: Path,
    artifact_id: str,
    *,
    expected_version: str | None = None,
    expected_requested_version: str | None = None,
    policy_root: Path | None = None,
    require_policy_bound: bool = False,
) -> Path:
    """Return one verified cache path or fail before a consumer can use it."""

    cache_root = cache_root.resolve()
    errors = validate_artifact_manifest(manifest_path, cache_root)
    if errors:
        raise LifecycleError("; ".join(errors))
    records, record_errors = _load_artifact_records(manifest_path)
    if record_errors:
        raise LifecycleError("; ".join(record_errors))
    record = records.get(artifact_id)
    if record is None:
        raise LifecycleError(f"artifact is absent from export manifest: {artifact_id}")
    if require_policy_bound:
        if policy_root is None:
            raise LifecycleError(
                "policy-bound artifact resolution requires a policy root"
            )
        policy_errors = validate_dependency_inputs(
            policy_root,
            artifact_manifest=manifest_path,
            cache_root=cache_root,
            reproducible=True,
            artifact_ids={artifact_id},
            include_models=artifact_id.startswith("model:"),
        )
        if policy_errors:
            raise LifecycleError("; ".join(policy_errors))
    if (
        expected_requested_version is not None
        and record.requested_version != expected_requested_version
    ):
        raise LifecycleError(
            f"{artifact_id}: cached requested version "
            f"{record.requested_version!r} does not match "
            f"{expected_requested_version!r}"
        )
    if expected_version is not None and record.resolved_version != expected_version:
        raise LifecycleError(
            f"{artifact_id}: cached version {record.resolved_version!r} "
            f"does not match expected version {expected_version!r}"
        )
    relative = validate_relative_path(record.cache_path)
    return cache_root / Path(*PurePosixPath(relative).parts)


def record_cached_artifact(
    cache_root: Path,
    *,
    artifact_id: str,
    source_file: Path,
    source_url: str,
    requested_version: str,
    resolved_version: str,
    trust: str = "untrusted",
) -> ArtifactRecord:
    """Copy a locally obtained artifact into a hashed, auditable export cache."""

    cache_root = cache_root.resolve()
    source_file = source_file.resolve()
    if not PORTABLE_ID_PATTERN.fullmatch(artifact_id):
        raise LifecycleError(f"invalid artifact id: {artifact_id!r}")
    if not source_file.is_file():
        raise LifecycleError(f"artifact source is not a file: {source_file}")
    if not _is_exact_pin(resolved_version):
        raise LifecycleError("resolved artifact version must be exact")
    if trust not in {"policy-bound", "untrusted"}:
        raise LifecycleError("artifact trust classification is invalid")
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme not in {"file", "https", "oci"}:
        raise LifecycleError("artifact source URL must use file, https, or oci")
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", artifact_id).strip("-")
    relative = PurePosixPath("files", safe_id, source_file.name).as_posix()
    validate_relative_path(relative)
    destination = cache_root / Path(*PurePosixPath(relative).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(destination, source_file.read_bytes())
    record = ArtifactRecord(
        artifact_id=artifact_id,
        cache_path=relative,
        source_url=source_url,
        requested_version=requested_version,
        resolved_version=resolved_version,
        sha256=_sha256_file(destination),
        size=destination.stat().st_size,
        recorded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        trust=trust,
    )
    manifest_path = cache_root / "manifest.json"
    records, errors = _load_artifact_records(
        manifest_path if manifest_path.is_file() else None
    )
    if errors:
        raise LifecycleError("; ".join(errors))
    records[artifact_id] = record
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifacts": [
            item.to_payload()
            for item in sorted(records.values(), key=lambda item: item.artifact_id)
        ],
    }
    atomic_write_bytes(manifest_path, _json_bytes(payload))
    validation = validate_artifact_manifest(manifest_path, cache_root)
    if validation:
        raise LifecycleError("; ".join(validation))
    return record


SOURCE_RECEIPT = ".oracle-source.json"


def _guard_source_destination(
    trusted_root: Path,
    destination: Path,
) -> tuple[Path, Path]:
    """Confine one source tree beneath an explicit, non-reparse root."""

    raw_root = Path(trusted_root)
    raw_destination = Path(destination)
    if ".." in raw_root.parts or ".." in raw_destination.parts:
        raise LifecycleError("source destination uses lexical traversal")
    trusted_root = Path(os.path.abspath(os.fspath(raw_root)))
    destination = Path(os.path.abspath(os.fspath(raw_destination)))
    try:
        relative = destination.relative_to(trusted_root)
    except ValueError as exc:
        raise LifecycleError(
            "source destination is outside the explicit trusted root"
        ) from exc
    if not relative.parts:
        raise LifecycleError("source destination must be beneath the trusted root")
    for candidate in [*reversed(trusted_root.parents), trusted_root]:
        if candidate == Path(candidate.anchor):
            continue
        if _is_reparse_point(candidate):
            raise LifecycleError(
                f"source trusted root has a symlink or reparse ancestor: {candidate}"
            )
        if candidate.exists() and not candidate.is_dir():
            raise LifecycleError(
                f"source trusted root ancestor is not a directory: {candidate}"
            )
    if not trusted_root.is_dir():
        raise LifecycleError(
            f"explicit source trusted root is missing: {trusted_root}"
        )
    real_root = Path(os.path.realpath(trusted_root))
    current = trusted_root
    for index, part in enumerate(relative.parts):
        current = current / part
        is_final = index == len(relative.parts) - 1
        if _is_reparse_point(current):
            role = "target" if is_final else "ancestor"
            raise LifecycleError(
                f"source destination has a symlink or reparse {role}: {current}"
            )
        if current.exists():
            if not current.is_dir():
                role = "target" if is_final else "ancestor"
                raise LifecycleError(
                    f"source destination {role} is not a directory: {current}"
                )
            resolved = Path(os.path.realpath(current))
            try:
                contained = (
                    os.path.commonpath((str(real_root), str(resolved)))
                    == str(real_root)
                )
            except ValueError:
                contained = False
            if not contained:
                raise LifecycleError(
                    f"source destination resolves outside trusted root: {current}"
                )
    return trusted_root, destination


def _create_source_parent(trusted_root: Path, destination: Path) -> None:
    """Create missing destination ancestors one at a time under a checked root."""

    trusted_root, destination = _guard_source_destination(
        trusted_root, destination
    )
    relative_parent = destination.parent.relative_to(trusted_root)
    current = trusted_root
    for part in relative_parent.parts:
        current = current / part
        if not current.exists():
            current.mkdir()
        _guard_source_destination(trusted_root, current / "_oracle_child_probe")
        if _is_reparse_point(current) or not current.is_dir():
            raise LifecycleError(
                f"source destination ancestor became unsafe: {current}"
            )
    _guard_source_destination(trusted_root, destination)


def _extract_source_archive(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    if archive_path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    relative = validate_relative_path(info.filename.rstrip("/"))
                    target = destination / Path(*PurePosixPath(relative).parts)
                    unix_type = (info.external_attr >> 16) & 0o170000
                    if unix_type == stat.S_IFLNK:
                        raise LifecycleError("source archive contains a symbolic link")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode:
                        os.chmod(target, mode)
        except (zipfile.BadZipFile, OSError) as exc:
            raise LifecycleError(f"invalid source zip archive: {archive_path}") from exc
    else:
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                for member in archive.getmembers():
                    relative = validate_relative_path(member.name.rstrip("/"))
                    target = destination / Path(*PurePosixPath(relative).parts)
                    if member.issym() or member.islnk():
                        raise LifecycleError("source archive contains a link")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise LifecycleError(
                            "source archive contains a non-file member"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise LifecycleError("source archive member is unreadable")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, member.mode & 0o777)
        except (tarfile.TarError, OSError) as exc:
            raise LifecycleError(f"invalid source tar archive: {archive_path}") from exc
    children = list(destination.iterdir())
    if len(children) == 1 and children[0].is_dir() and not _is_reparse_point(children[0]):
        return children[0]
    return destination


def _source_tree_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_root).as_posix()
        if relative == SOURCE_RECEIPT:
            continue
        if _is_reparse_point(path):
            raise LifecycleError(f"installed source tree contains a reparse point: {path}")
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        if path.is_dir():
            digest.update(b"D")
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            digest.update(bytes.fromhex(_sha256_file(path)))
        else:
            raise LifecycleError(f"installed source tree has an unsafe entry: {path}")
    return digest.hexdigest()


def _expected_source_tree(
    archive_path: Path,
    parent: Path | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(
        prefix=".source-validate-",
        dir=parent,
    )
    temporary_root = Path(temporary.name)
    try:
        content_root = _extract_source_archive(
            archive_path, temporary_root / "extracted"
        )
    except Exception:
        temporary.cleanup()
        raise
    return temporary, content_root


def _source_receipt(
    destination: Path,
    artifact_id: str,
) -> dict[str, Any]:
    receipt_path = destination / SOURCE_RECEIPT
    if _is_reparse_point(receipt_path) or not receipt_path.is_file():
        raise LifecycleError(
            f"source destination is unowned; {SOURCE_RECEIPT} is missing or unsafe"
        )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("source destination has an invalid ownership receipt") from exc
    required = {
        "schema_version",
        "artifact_id",
        "requested_version",
        "resolved_version",
        "source_url",
        "archive_sha256",
        "tree_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise LifecycleError("source destination has an invalid ownership receipt")
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("artifact_id") != artifact_id
        or not _is_exact_pin(str(receipt.get("resolved_version", "")))
        or not SHA256_PATTERN.fullmatch(str(receipt.get("archive_sha256", "")))
        or not SHA256_PATTERN.fullmatch(str(receipt.get("tree_sha256", "")))
    ):
        raise LifecycleError("source destination has an invalid ownership receipt")
    parsed = urllib.parse.urlparse(str(receipt.get("source_url", "")))
    if parsed.scheme not in {"https", "oci"} or parsed.username or parsed.password:
        raise LifecycleError("source destination has an invalid ownership receipt")
    if _source_tree_digest(destination) != receipt["tree_sha256"]:
        raise LifecycleError(
            "installed source tree digest mismatch; source destination was "
            "modified after its owned fingerprint was recorded"
        )
    return receipt


def _source_receipt_payload(
    artifact_id: str,
    record: ArtifactRecord,
    tree_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "requested_version": record.requested_version,
        "resolved_version": record.resolved_version,
        "source_url": record.source_url,
        "archive_sha256": record.sha256,
        "tree_sha256": tree_digest,
    }


def _source_install_preflight(
    root: Path,
    manifest_path: Path,
    cache_root: Path,
    artifact_id: str,
    destination: Path,
    *,
    trusted_root: Path,
    expected_version: str,
    expected_requested_version: str,
) -> tuple[Path, ArtifactRecord, dict[str, Any] | None, str]:
    """Validate policy, archive, containment, and prior ownership without mutation."""

    root = root.resolve()
    _trusted_root, destination = _guard_source_destination(
        trusted_root, destination
    )
    archive_path = resolve_cached_artifact(
        manifest_path,
        cache_root,
        artifact_id,
        expected_version=expected_version,
        expected_requested_version=expected_requested_version,
        policy_root=root,
        require_policy_bound=True,
    )
    records, errors = _load_artifact_records(manifest_path)
    if errors:
        raise LifecycleError("; ".join(errors))
    record = records[artifact_id]
    previous = (
        _source_receipt(destination, artifact_id)
        if destination.exists()
        else None
    )
    temporary, expected_root = _expected_source_tree(archive_path)
    try:
        expected_tree_digest = _source_tree_digest(expected_root)
    finally:
        temporary.cleanup()
    return archive_path, record, previous, expected_tree_digest


def preflight_source_install(
    root: Path,
    manifest_path: Path,
    cache_root: Path,
    artifact_id: str,
    destination: Path,
    *,
    trusted_root: Path,
    expected_version: str,
    expected_requested_version: str,
) -> Path:
    """Validate a source install or upgrade before any target mutation."""

    _source_install_preflight(
        root,
        manifest_path,
        cache_root,
        artifact_id,
        destination,
        trusted_root=trusted_root,
        expected_version=expected_version,
        expected_requested_version=expected_requested_version,
    )
    return Path(os.path.abspath(os.fspath(destination)))


def install_source_archive(
    root: Path,
    manifest_path: Path,
    cache_root: Path,
    artifact_id: str,
    destination: Path,
    *,
    trusted_root: Path,
    expected_version: str,
    expected_requested_version: str,
) -> Path:
    """Install or safely upgrade a source tree from a policy-bound export."""

    archive_path, record, previous, expected_tree_digest = _source_install_preflight(
        root,
        manifest_path,
        cache_root,
        artifact_id,
        destination,
        trusted_root=trusted_root,
        expected_version=expected_version,
        expected_requested_version=expected_requested_version,
    )
    trusted_root, destination = _guard_source_destination(
        trusted_root, destination
    )
    desired_receipt = _source_receipt_payload(
        artifact_id, record, expected_tree_digest
    )
    if previous == desired_receipt:
        return destination
    _create_source_parent(trusted_root, destination)
    trusted_root, destination = _guard_source_destination(
        trusted_root, destination
    )
    temporary, content_root = _expected_source_tree(archive_path, destination.parent)
    backup_temporary: Path | None = None
    backup: Path | None = None
    try:
        tree_digest = _source_tree_digest(content_root)
        if tree_digest != expected_tree_digest:
            raise LifecycleError("staged source tree differs from validated archive")
        atomic_write_bytes(
            content_root / SOURCE_RECEIPT,
            _json_bytes(desired_receipt),
        )
        _guard_source_destination(trusted_root, destination)
        if destination.exists():
            current = _source_receipt(destination, artifact_id)
            if current != previous:
                raise LifecycleError(
                    "source destination changed after upgrade preflight"
                )
            backup_temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.backup-",
                    dir=destination.parent,
                )
            )
            _guard_source_destination(trusted_root, backup_temporary)
            backup = backup_temporary / "previous"
            os.replace(destination, backup)
        try:
            os.replace(content_root, destination)
            validation = validate_source_install(
                root,
                manifest_path,
                cache_root,
                artifact_id,
                destination,
                trusted_root=trusted_root,
                expected_version=expected_version,
                expected_requested_version=expected_requested_version,
            )
            if validation:
                raise LifecycleError("; ".join(validation))
        except Exception:
            if destination.exists() and not _is_reparse_point(destination):
                os.replace(destination, content_root)
            restored_previous = False
            if backup is not None and backup.exists():
                os.replace(backup, destination)
                restored_previous = True
            if (
                restored_previous
                and backup_temporary is not None
                and backup_temporary.exists()
                and not _is_reparse_point(backup_temporary)
            ):
                try:
                    backup_temporary.rmdir()
                except OSError:
                    # Preserve a nonempty or concurrently changed backup root.
                    pass
            raise
        if backup is not None and backup.exists():
            if (
                previous is None
                or _source_tree_digest(backup) != previous["tree_sha256"]
            ):
                raise LifecycleError(
                    f"previous owned source changed during upgrade; preserved at {backup}"
                )
            shutil.rmtree(backup)
        if backup_temporary is not None and backup_temporary.exists():
            backup_temporary.rmdir()
    finally:
        temporary.cleanup()
    return destination


def validate_source_install(
    root: Path,
    manifest_path: Path,
    cache_root: Path,
    artifact_id: str,
    destination: Path,
    *,
    trusted_root: Path,
    expected_version: str,
    expected_requested_version: str,
) -> list[str]:
    """Validate source identity, archive bytes, receipt, and extracted tree bytes."""

    errors: list[str] = []
    try:
        _trusted_root, destination = _guard_source_destination(
            trusted_root, destination
        )
        archive_path = resolve_cached_artifact(
            manifest_path,
            cache_root,
            artifact_id,
            expected_version=expected_version,
            expected_requested_version=expected_requested_version,
            policy_root=root,
            require_policy_bound=True,
        )
        records, record_errors = _load_artifact_records(manifest_path)
        if record_errors:
            raise LifecycleError("; ".join(record_errors))
        record = records[artifact_id]
        if not destination.is_dir():
            raise LifecycleError("installed source tree is missing or unsafe")
        receipt = _source_receipt(destination, artifact_id)
        temporary, expected_root = _expected_source_tree(archive_path)
        try:
            expected_tree_digest = _source_tree_digest(expected_root)
        finally:
            temporary.cleanup()
        expected_receipt = _source_receipt_payload(
            artifact_id, record, expected_tree_digest
        )
        if receipt != expected_receipt:
            errors.append(f"{artifact_id}: installed source receipt mismatch")
    except (
        LifecycleError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        errors.append(f"{artifact_id}: installed source validation failed: {exc}")
    return errors


def _load_dependency_policy(
    root: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    try:
        versions, errors = _parse_versions_text(
            (root / "VERSIONS.lock").read_text(encoding="utf-8")
        )
        if errors:
            raise LifecycleError("; ".join(errors))
        policy = json.loads(
            (root / "verification" / "policy.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"dependency policy is unreadable: {exc}") from exc
    if not isinstance(policy, dict):
        raise LifecycleError("dependency policy must be an object")
    policy_errors = _dependency_policy_errors(versions, policy)
    if policy_errors:
        raise LifecycleError("; ".join(policy_errors))
    return versions, policy


def _normalize_dependency_authority(
    artifact_id: str,
    raw: Mapping[str, Any],
    *,
    allow_file: bool = False,
) -> dict[str, str]:
    required = {
        "kind",
        "requested_version",
        "resolved_version",
        "source_url",
        "sha256",
    }
    if set(raw) != required:
        raise LifecycleError(
            f"{artifact_id}: dependency authority fields are incomplete"
        )
    kind = raw.get("kind")
    requested = raw.get("requested_version")
    resolved = raw.get("resolved_version")
    source_url = raw.get("source_url")
    digest = str(raw.get("sha256", "")).lower()
    if (
        kind not in DEPENDENCY_KINDS
        or not isinstance(requested, str)
        or not requested
        or not isinstance(resolved, str)
        or not _is_exact_pin(resolved)
        or not isinstance(source_url, str)
        or not source_url
        or not SHA256_PATTERN.fullmatch(digest)
    ):
        if not SHA256_PATTERN.fullmatch(digest):
            raise LifecycleError(
                f"{artifact_id}: independently expected SHA-256 is invalid"
            )
        raise LifecycleError(f"{artifact_id}: dependency authority is invalid")
    parsed = urllib.parse.urlparse(source_url)
    if parsed.username or parsed.password:
        raise LifecycleError(
            f"{artifact_id}: authority source URL embeds credentials"
        )
    if kind == "container":
        if not re.fullmatch(
            r"oci://[^@\s]+@sha256:[0-9a-f]{64}",
            source_url,
        ):
            raise LifecycleError(
                f"{artifact_id}: container authority is not digest-immutable"
            )
    elif parsed.scheme != "https" and not (
        allow_file and parsed.scheme == "file"
    ):
        raise LifecycleError(
            f"{artifact_id}: authority source URL must use HTTPS"
        )
    if kind == "git" and not COMMIT_PATTERN.fullmatch(resolved):
        raise LifecycleError(
            f"{artifact_id}: Git authority lacks an immutable commit"
        )
    return {
        "kind": str(kind),
        "requested_version": requested,
        "resolved_version": resolved,
        "source_url": source_url,
        "sha256": digest,
    }


def _dependency_authorities_from_payload(
    payload: Any,
) -> dict[str, dict[str, str]]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(payload.get("authorities"), dict)
    ):
        raise LifecycleError("dependency authority manifest has an unsupported schema")
    authorities: dict[str, dict[str, str]] = {}
    for artifact_id, raw in payload["authorities"].items():
        if (
            not isinstance(artifact_id, str)
            or not PORTABLE_ID_PATTERN.fullmatch(artifact_id)
            or not isinstance(raw, dict)
        ):
            raise LifecycleError(
                "dependency authority manifest contains an invalid entry"
            )
        authorities[artifact_id] = _normalize_dependency_authority(
            artifact_id, raw
        )
    return authorities


def _load_dependency_authorities(
    root: Path,
) -> dict[str, dict[str, str]]:
    path = root / DEPENDENCY_AUTHORITY_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"dependency authority manifest is unreadable: {exc}") from exc
    return _dependency_authorities_from_payload(payload)


def _validate_dependency_authority_policy(
    versions: Mapping[str, str],
    raw: Mapping[str, Any],
    authority: Mapping[str, Any],
    *,
    allow_file: bool = False,
) -> dict[str, str]:
    artifact_id = str(raw.get("id", ""))
    normalized = _normalize_dependency_authority(
        artifact_id,
        authority,
        allow_file=allow_file,
    )
    kind = str(raw.get("kind", ""))
    version_key = str(raw.get("version_key", ""))
    requested = versions.get(version_key, "")
    if normalized["kind"] != kind:
        raise LifecycleError(
            f"{artifact_id}: promoted authority kind differs from policy"
        )
    if normalized["requested_version"] != requested:
        raise LifecycleError(
            f"{artifact_id}: promoted authority request differs from {version_key}"
        )
    source = raw.get("source")
    if not isinstance(source, dict):
        raise LifecycleError(
            f"{artifact_id}: authoritative source policy is missing"
        )
    locked_digest = _trusted_digest(source, versions)
    if (
        SHA256_PATTERN.fullmatch(locked_digest)
        and normalized["sha256"] != locked_digest
    ):
        raise LifecycleError(
            f"{artifact_id}: promoted digest differs from VERSIONS.lock"
        )
    locked_resolved = _trusted_resolved_version(source, versions, requested)
    if kind == "git":
        locked_resolved = _trusted_revision(source, versions)
    if (
        _is_exact_pin(locked_resolved)
        and normalized["resolved_version"] != locked_resolved
    ):
        raise LifecycleError(
            f"{artifact_id}: promoted resolved identity differs from VERSIONS.lock"
        )
    identity = _source_identity(
        source,
        versions,
        requested_version=requested,
        resolved_version=normalized["resolved_version"],
    )
    expected_url = _authoritative_source_url(
        source,
        versions,
        requested_version=requested,
        resolved_version=normalized["resolved_version"],
    )
    unresolved_identity = (
        not identity
        or identity in UNRESOLVED_VALUES
        or "unresolved" in identity
        or "unresolved" in expected_url
    )
    if (
        not unresolved_identity
        and normalized["source_url"] != expected_url
    ):
        raise LifecycleError(
            f"{artifact_id}: promoted source differs from VERSIONS.lock identity"
        )
    if kind == "container":
        identity_digest_key = source.get("identity_digest_key")
        locked_identity_digest = (
            versions.get(identity_digest_key, "")
            if isinstance(identity_digest_key, str)
            else ""
        )
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", locked_identity_digest)
            and not normalized["source_url"].endswith(
                f"@{locked_identity_digest}"
            )
        ):
            raise LifecycleError(
                f"{artifact_id}: promoted container digest differs from VERSIONS.lock"
            )
    return normalized


def _effective_dependency_authority(
    root: Path,
    versions: Mapping[str, str],
    raw: Mapping[str, Any],
    *,
    authorities: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, str]:
    artifact_id = str(raw.get("id", ""))
    available = (
        _load_dependency_authorities(root)
        if authorities is None
        else authorities
    )
    promoted = available.get(artifact_id)
    if promoted is not None:
        return _validate_dependency_authority_policy(
            versions, raw, promoted
        )
    version_key = str(raw.get("version_key", ""))
    requested = versions.get(version_key, "")
    source = raw.get("source")
    if not isinstance(source, dict):
        raise LifecycleError(
            f"{artifact_id}: authoritative source policy is missing"
        )
    digest = _trusted_digest(source, versions)
    if not SHA256_PATTERN.fullmatch(digest):
        raise LifecycleError(
            f"{artifact_id}: immutable trusted digest is unresolved"
        )
    kind = str(raw.get("kind", ""))
    resolved = _trusted_resolved_version(source, versions, requested)
    if kind == "git":
        resolved = _trusted_revision(source, versions)
        if not COMMIT_PATTERN.fullmatch(resolved):
            raise LifecycleError(
                f"{artifact_id}: immutable trusted revision is unresolved"
            )
    elif not _is_exact_pin(resolved):
        raise LifecycleError(
            f"{artifact_id}: trusted resolved version is unresolved"
        )
    source_url = _authoritative_source_url(
        source,
        versions,
        requested_version=requested,
        resolved_version=resolved,
    )
    identity = _source_identity(
        source,
        versions,
        requested_version=requested,
        resolved_version=resolved,
    )
    if (
        not identity
        or identity in UNRESOLVED_VALUES
        or "unresolved" in identity
        or not source_url
        or "unresolved" in source_url
    ):
        raise LifecycleError(
            f"{artifact_id}: authoritative source identity is unresolved"
        )
    return _validate_dependency_authority_policy(
        versions,
        raw,
        {
            "kind": kind,
            "requested_version": requested,
            "resolved_version": resolved,
            "source_url": source_url,
            "sha256": digest,
        },
        allow_file=True,
    )


def promote_dependency_authority(
    root: Path,
    *,
    artifact_id: str,
    authority_file: Path,
) -> dict[str, str]:
    """Promote independently supplied identity/digest data, never artifact bytes."""

    root = root.resolve()
    tracked_path = root / DEPENDENCY_AUTHORITY_FILE
    if authority_file.resolve() == tracked_path.resolve():
        raise LifecycleError(
            "promotion requires a separate independently supplied authority file"
        )
    try:
        supplied_payload = json.loads(authority_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"supplied dependency authority is unreadable: {exc}") from exc
    supplied = _dependency_authorities_from_payload(supplied_payload).get(
        artifact_id
    )
    if supplied is None:
        raise LifecycleError(
            f"supplied dependency authority lacks {artifact_id}"
        )
    versions, policy = _load_dependency_policy(root)
    raw_inputs = policy.get("dependency_inputs", [])
    raw = next(
        (
            item
            for item in raw_inputs
            if isinstance(item, dict) and item.get("id") == artifact_id
        ),
        None,
    )
    if raw is None:
        raise LifecycleError(
            f"{artifact_id}: absent from authoritative dependency policy"
        )
    promoted = _validate_dependency_authority_policy(
        versions, raw, supplied
    )
    authorities = _load_dependency_authorities(root)
    authorities[artifact_id] = promoted
    payload = {
        "schema_version": SCHEMA_VERSION,
        "authorities": {
            key: authorities[key] for key in sorted(authorities)
        },
    }
    atomic_write_bytes(tracked_path, _json_bytes(payload))
    return promoted


def _trusted_export_digest(
    root: Path,
    *,
    artifact_id: str,
    source_url: str,
    requested_version: str,
    resolved_version: str,
) -> str:
    versions, policy = _load_dependency_policy(root.resolve())
    raw_inputs = policy.get("dependency_inputs", [])
    raw = next(
        (
            item
            for item in raw_inputs
            if isinstance(item, dict) and item.get("id") == artifact_id
        ),
        None,
    )
    if raw is None:
        raise LifecycleError(f"{artifact_id}: absent from authoritative dependency policy")
    authority = _effective_dependency_authority(
        root.resolve(), versions, raw
    )
    if requested_version != authority["requested_version"]:
        raise LifecycleError(
            f"{artifact_id}: requested version differs from authoritative policy"
        )
    if resolved_version != authority["resolved_version"]:
        raise LifecycleError(
            f"{artifact_id}: resolved version differs from authoritative policy"
        )
    if source_url != authority["source_url"]:
        raise LifecycleError(
            f"{artifact_id}: URL differs from authoritative source identity"
        )
    return authority["sha256"]


def import_artifact(
    root: Path,
    cache_root: Path,
    *,
    artifact_id: str,
    source_file: Path,
    source_url: str,
    requested_version: str,
    resolved_version: str,
) -> ArtifactRecord:
    """Import a local file only when tracked policy independently binds its bytes."""

    expected_digest = _trusted_export_digest(
        root,
        artifact_id=artifact_id,
        source_url=source_url,
        requested_version=requested_version,
        resolved_version=resolved_version,
    )
    actual_digest = _sha256_file(source_file)
    if actual_digest != expected_digest:
        raise LifecycleError(
            f"{artifact_id}: imported content differs from trusted digest"
        )
    return record_cached_artifact(
        cache_root,
        artifact_id=artifact_id,
        source_file=source_file,
        source_url=source_url,
        requested_version=requested_version,
        resolved_version=resolved_version,
        trust="policy-bound",
    )


def export_artifact(
    cache_root: Path,
    *,
    artifact_id: str,
    source_url: str,
    requested_version: str,
    resolved_version: str,
    expected_sha256: str | None = None,
    policy_root: Path | None = None,
    trusted: bool = False,
) -> ArtifactRecord:
    """Acquire one artifact, optionally binding it to committed source policy."""

    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme not in {"file", "https"}:
        raise LifecycleError("artifact source URL must use file or https")
    if parsed.username or parsed.password:
        raise LifecycleError("artifact source URL must not embed credentials")
    if expected_sha256 is not None and not SHA256_PATTERN.fullmatch(
        expected_sha256.lower()
    ):
        raise LifecycleError("expected SHA-256 must be 64 hexadecimal characters")
    policy_digest: str | None = None
    if trusted:
        if policy_root is None:
            raise LifecycleError("trusted export requires an authoritative policy root")
        policy_digest = _trusted_export_digest(
            policy_root,
            artifact_id=artifact_id,
            source_url=source_url,
            requested_version=requested_version,
            resolved_version=resolved_version,
        )
        if (
            expected_sha256 is not None
            and expected_sha256.lower() != policy_digest
        ):
            raise LifecycleError(
                "caller-supplied hash differs from authoritative trusted digest"
            )
    filename = Path(urllib.parse.unquote(parsed.path)).name or "artifact.bin"
    if any(ord(character) < 32 for character in filename):
        raise LifecycleError("artifact URL contains an unsafe file name")
    cache_root = cache_root.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".dependency-export-", dir=cache_root
    ) as temporary_directory:
        temporary = Path(temporary_directory) / filename
        with temporary.open("wb") as output:
            try:
                with urllib.request.urlopen(source_url, timeout=120) as response:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
            except Exception as exc:
                raise LifecycleError(f"artifact fetch failed: {exc}") from exc
            output.flush()
            os.fsync(output.fileno())
        actual = _sha256_file(temporary)
        comparison_digest = policy_digest or (
            expected_sha256.lower() if expected_sha256 is not None else None
        )
        if comparison_digest is not None and actual != comparison_digest:
            raise LifecycleError(
                f"expected SHA-256 {comparison_digest}, downloaded {actual}"
            )
        return record_cached_artifact(
            cache_root,
            artifact_id=artifact_id,
            source_file=temporary,
            source_url=source_url,
            requested_version=requested_version,
            resolved_version=resolved_version,
            trust="policy-bound" if trusted else "untrusted",
        )


def record_model_snapshot(
    root: Path,
    cache_root: Path,
    *,
    models_root: Path | None = None,
    model_name: str,
    repository: str,
    requested_revision: str,
    resolved_revision: str,
) -> ArtifactRecord:
    """Record untrusted acquisition evidence for local GGUF shards."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name):
        raise LifecycleError("model name is not portable")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*",
        repository,
    ):
        raise LifecycleError("model repository must be an owner/name identifier")
    root = root.resolve()
    cache_root = cache_root.resolve()
    declared = _declared_model(
        root,
        model_name,
        repository,
        requested_revision,
        resolved_revision,
    )
    model_base = models_root.resolve() if models_root is not None else root / "models"
    model_root = model_base / model_name
    files: list[dict[str, Any]] = []
    for relative, path in _local_model_files(model_root, declared["include"]):
        files.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": f"model:{model_name}",
        "model_name": model_name,
        "repository": repository,
        "include": declared["include"],
        "requested_revision": requested_revision,
        "resolved_revision": resolved_revision,
        "files": files,
    }
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".model-snapshot-", dir=cache_root
    ) as temporary_directory:
        snapshot = Path(temporary_directory) / f"{model_name}.model.json"
        atomic_write_bytes(snapshot, _json_bytes(payload))
        return record_cached_artifact(
            cache_root,
            artifact_id=f"model:{model_name}",
            source_file=snapshot,
            source_url=(
                f"https://huggingface.co/{repository}/tree/{resolved_revision}"
            ),
            requested_version=requested_revision,
            resolved_version=resolved_revision,
            trust="untrusted",
        )


def import_model_snapshot(
    root: Path,
    cache_root: Path,
    *,
    model_name: str,
    authority_file: Path,
    models_root: Path | None = None,
) -> ArtifactRecord:
    """Import independently supplied, tracked model identities and shard hashes."""

    root = root.resolve()
    cache_root = cache_root.resolve()
    tracked_authority_path = root / "serving" / "model-authorities.json"
    if authority_file.resolve() == tracked_authority_path.resolve():
        raise LifecycleError(
            "model import requires a separate independently supplied authority file"
        )
    try:
        supplied_payload = json.loads(authority_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"supplied model authority is unreadable: {exc}") from exc
    if (
        not isinstance(supplied_payload, dict)
        or supplied_payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(supplied_payload.get("models"), dict)
    ):
        raise LifecycleError("supplied model authority has an unsupported schema")
    supplied = supplied_payload["models"].get(model_name)
    tracked = _model_authorities(root).get(model_name)
    if not isinstance(supplied, dict) or tracked is None:
        raise LifecycleError(
            f"model authority is not promoted in tracked policy: {model_name}"
        )
    normalized_supplied = {
        "repository": supplied.get("repository"),
        "revision": supplied.get("revision"),
        "include": supplied.get("include"),
        "files": sorted(
            supplied.get("files", []),
            key=lambda item: str(item.get("path", "")) if isinstance(item, dict) else "",
        ),
    }
    if normalized_supplied != tracked:
        raise LifecycleError(
            f"supplied model authority differs from tracked policy: {model_name}"
        )
    declared = _declared_model(
        root,
        model_name,
        tracked["repository"],
        tracked["revision"],
        tracked["revision"],
    )
    if declared["include"] != tracked["include"]:
        raise LifecycleError("model authority include differs from models.manifest")
    model_base = models_root.resolve() if models_root is not None else root / "models"
    model_root = model_base / model_name
    local_files = {
        relative: path
        for relative, path in _local_model_files(model_root, tracked["include"])
    }
    expected_files = {
        str(item["path"]): item for item in tracked["files"]
    }
    if set(local_files) != set(expected_files):
        missing = sorted(set(expected_files) - set(local_files))
        unexpected = sorted(set(local_files) - set(expected_files))
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unexpected:
            detail.append("unexpected " + ", ".join(unexpected))
        raise LifecycleError("model authority file set mismatch: " + "; ".join(detail))
    for relative, expected in expected_files.items():
        path = local_files[relative]
        if path.stat().st_size != expected["size"]:
            raise LifecycleError(f"model size mismatch: {relative}")
        if _sha256_file(path) != expected["sha256"]:
            raise LifecycleError(f"model checksum mismatch: {relative}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": f"model:{model_name}",
        "model_name": model_name,
        "repository": tracked["repository"],
        "include": tracked["include"],
        "requested_revision": tracked["revision"],
        "resolved_revision": tracked["revision"],
        "authority_sha256": _model_authority_digest(tracked),
        "files": tracked["files"],
    }
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".model-authority-", dir=cache_root
    ) as temporary_directory:
        snapshot = Path(temporary_directory) / f"{model_name}.model.json"
        atomic_write_bytes(snapshot, _json_bytes(payload))
        return record_cached_artifact(
            cache_root,
            artifact_id=f"model:{model_name}",
            source_file=snapshot,
            source_url=(
                f"https://huggingface.co/{tracked['repository']}/tree/"
                f"{tracked['revision']}"
            ),
            requested_version=tracked["revision"],
            resolved_version=tracked["revision"],
            trust="policy-bound",
        )


def validate_model_snapshot(
    root: Path,
    cache_root: Path,
    record: ArtifactRecord,
) -> list[str]:
    """Validate one cached model snapshot against the local model files."""

    errors: list[str] = []
    if not record.artifact_id.startswith("model:"):
        return [f"{record.artifact_id}: not a model artifact"]
    try:
        relative = validate_relative_path(record.cache_path)
    except LifecycleError as exc:
        return [f"{record.artifact_id}: {exc}"]
    snapshot_path = cache_root.resolve() / Path(*PurePosixPath(relative).parts)
    if not snapshot_path.is_file() or snapshot_path.is_symlink():
        return [f"{record.artifact_id}: model snapshot manifest is missing or unsafe"]
    if (
        snapshot_path.stat().st_size != record.size
        or _sha256_file(snapshot_path) != record.sha256
    ):
        return [f"{record.artifact_id}: model snapshot manifest checksum mismatch"]
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{record.artifact_id}: invalid model snapshot manifest: {exc}"]
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return [f"{record.artifact_id}: unsupported model snapshot schema"]
    model_name = payload.get("model_name")
    if (
        not isinstance(model_name, str)
        or record.artifact_id != f"model:{model_name}"
        or payload.get("resolved_revision") != record.resolved_version
    ):
        errors.append(f"{record.artifact_id}: model snapshot identity mismatch")
        return errors
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return [f"{record.artifact_id}: model snapshot has no files"]
    authority: dict[str, Any] | None = None
    if record.trust == "policy-bound":
        try:
            authority = _model_authorities(root).get(model_name)
        except LifecycleError as exc:
            return [f"{record.artifact_id}: {exc}"]
        if authority is None:
            return [f"{record.artifact_id}: policy-bound model authority is missing"]
        try:
            declared = _declared_models(root).get(model_name)
        except LifecycleError as exc:
            return [f"{record.artifact_id}: {exc}"]
        if (
            declared is None
            or declared["repository"] != authority["repository"]
            or declared["revision"] != authority["revision"]
            or declared["include"] != authority["include"]
            or payload.get("repository") != authority["repository"]
            or payload.get("include") != authority["include"]
            or payload.get("requested_revision") != authority["revision"]
            or payload.get("resolved_revision") != authority["revision"]
            or payload.get("authority_sha256")
            != _model_authority_digest(authority)
            or raw_files != authority["files"]
        ):
            return [f"{record.artifact_id}: policy-bound model authority mismatch"]
    model_root = root.resolve() / "models" / model_name
    seen: set[str] = set()
    for index, raw in enumerate(raw_files):
        if not isinstance(raw, dict):
            errors.append(f"{record.artifact_id}: file {index} is not an object")
            continue
        try:
            path_text = validate_relative_path(str(raw.get("path", "")))
        except LifecycleError as exc:
            errors.append(f"{record.artifact_id}: file {index}: {exc}")
            continue
        if path_text in seen:
            errors.append(f"{record.artifact_id}: duplicate model file {path_text}")
            continue
        seen.add(path_text)
        expected_hash = str(raw.get("sha256", ""))
        expected_size = raw.get("size")
        target = model_root / Path(*PurePosixPath(path_text).parts)
        if target.is_symlink() or not target.is_file():
            errors.append(f"{record.artifact_id}: model file is missing: {path_text}")
        elif target.stat().st_size != expected_size:
            errors.append(f"{record.artifact_id}: size mismatch: {path_text}")
        elif (
            not SHA256_PATTERN.fullmatch(expected_hash)
            or _sha256_file(target) != expected_hash
        ):
            errors.append(f"{record.artifact_id}: checksum mismatch: {path_text}")
    if authority is not None:
        try:
            actual_files = {
                relative
                for relative, _path in _local_model_files(
                    model_root, authority["include"]
                )
            }
        except LifecycleError as exc:
            errors.append(f"{record.artifact_id}: {exc}")
        else:
            unexpected = actual_files - seen
            for relative in sorted(unexpected):
                errors.append(
                    f"{record.artifact_id}: unexpected model file: {relative}"
                )
    return errors


def validated_model_paths(
    root: Path,
    cache_root: Path,
    model_name: str,
) -> list[Path]:
    """Return only shards authorized by tracked identities and a trusted cache record."""

    root = root.resolve()
    cache_root = cache_root.resolve()
    manifest_path = cache_root / "manifest.json"
    try:
        snapshot_path = resolve_cached_artifact(
            manifest_path,
            cache_root,
            f"model:{model_name}",
            policy_root=root,
            require_policy_bound=True,
        )
    except LifecycleError as exc:
        raise LifecycleError(
            f"policy-bound model snapshot is unavailable for {model_name}: {exc}"
        ) from exc
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(
            f"policy-bound model snapshot is unreadable for {model_name}: {exc}"
        ) from exc
    paths = [
        root / "models" / model_name / Path(*PurePosixPath(item["path"]).parts)
        for item in payload["files"]
    ]
    if not paths:
        raise LifecycleError(f"policy-bound model snapshot has no files: {model_name}")
    return paths


def preferred_model_path(root: Path, cache_root: Path, model_name: str) -> Path:
    paths = validated_model_paths(root, cache_root, model_name)
    first_shards = [
        path for path in paths if re.search(r"-00001-of-[0-9]+\.gguf$", path.name)
    ]
    return sorted(first_shards or paths, key=lambda path: str(path))[0]


def validate_dependency_inputs(
    root: Path,
    *,
    artifact_manifest: Path | None = None,
    cache_root: Path | None = None,
    reproducible: bool = False,
    artifact_ids: set[str] | None = None,
    include_models: bool = True,
    include_optional: bool = False,
) -> list[str]:
    """Validate central pins and, in reproducible mode, exact cached artifacts."""

    root = root.resolve()
    errors: list[str] = []
    versions_path = root / "VERSIONS.lock"
    policy_path = root / "verification" / "policy.json"
    models_path = root / "serving" / "models.manifest"
    try:
        versions, parse_errors = _parse_versions_text(
            versions_path.read_text(encoding="utf-8")
        )
        errors.extend(parse_errors)
    except (OSError, UnicodeDecodeError) as exc:
        versions = {}
        errors.append(f"VERSIONS.lock is unreadable: {exc}")
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if not isinstance(policy, dict):
            raise ValueError("expected an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        policy = {}
        errors.append(f"verification/policy.json is invalid: {exc}")
    try:
        models, model_errors = _parse_models_text(
            models_path.read_text(encoding="utf-8")
        )
        errors.extend(model_errors)
    except (OSError, UnicodeDecodeError) as exc:
        models = []
        errors.append(f"serving/models.manifest is unreadable: {exc}")

    pyproject_path = root / "env" / "pyproject.toml"
    uv_lock_path = root / "env" / "uv.lock"
    if pyproject_path.is_file() and not uv_lock_path.is_file():
        errors.append("env/uv.lock is required for the Python environment")
    elif uv_lock_path.is_file():
        try:
            uv_lock = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
            if uv_lock.get("version") != 1:
                errors.append("env/uv.lock has an unsupported schema version")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"env/uv.lock is invalid: {exc}")

    records, manifest_errors = _load_artifact_records(artifact_manifest)
    errors.extend(manifest_errors)
    if artifact_manifest is not None:
        errors.extend(
            validate_artifact_manifest(
                artifact_manifest, cache_root or artifact_manifest.parent
            )
        )

    errors.extend(_dependency_policy_errors(versions, policy))
    try:
        authorities = _load_dependency_authorities(root)
    except LifecycleError as exc:
        authorities = {}
        errors.append(str(exc))
    raw_inputs = policy.get("dependency_inputs", [])
    if not isinstance(raw_inputs, list):
        raw_inputs = []
    raw_by_id = {
        str(raw.get("id")): raw
        for raw in raw_inputs
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    for artifact_id, authority in authorities.items():
        raw = raw_by_id.get(artifact_id)
        if raw is None:
            errors.append(
                f"{artifact_id}: promoted authority has no dependency policy entry"
            )
            continue
        try:
            _validate_dependency_authority_policy(
                versions, raw, authority
            )
        except LifecycleError as exc:
            errors.append(str(exc))
    for raw in raw_inputs:
        if not isinstance(raw, dict):
            continue
        artifact_id = raw.get("id")
        version_key = raw.get("version_key")
        if (
            not isinstance(artifact_id, str)
            or not PORTABLE_ID_PATTERN.fullmatch(artifact_id)
            or not isinstance(version_key, str)
        ):
            continue
        if artifact_ids is not None and artifact_id not in artifact_ids:
            continue
        if (
            raw.get("optional", False)
            and artifact_ids is None
            and not include_optional
        ):
            continue
        value = versions.get(version_key)
        if value is None:
            continue
        dynamic = value in {"dynamic", "unresolved"}
        if reproducible:
            source = raw.get("source")
            if (
                artifact_id not in authorities
                and isinstance(source, dict)
                and raw.get("kind") == "git"
                and not COMMIT_PATTERN.fullmatch(
                    _trusted_revision(source, versions)
                )
            ):
                errors.append(
                    f"{artifact_id}: immutable trusted revision is unresolved"
                )
            if (
                artifact_id not in authorities
                and isinstance(source, dict)
                and raw.get("kind") == "container"
            ):
                identity_digest_key = source.get("identity_digest_key")
                identity_digest = (
                    versions.get(identity_digest_key, "")
                    if isinstance(identity_digest_key, str)
                    else ""
                )
                if not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", identity_digest
                ):
                    errors.append(
                        f"{artifact_id}: immutable container identity "
                        "digest is unresolved"
                    )
            try:
                authority = _effective_dependency_authority(
                    root,
                    versions,
                    raw,
                    authorities=authorities,
                )
            except LifecycleError as exc:
                authority = None
                errors.append(str(exc))
            record = records.get(artifact_id)
            if record is None:
                errors.append(f"{artifact_id}: reproducible mode needs a resolved artifact")
                continue
            if record.trust != "policy-bound":
                errors.append(
                    f"{artifact_id}: untrusted acquisition evidence is not reproducible"
                )
            if authority is None:
                continue
            if record.sha256 != authority["sha256"]:
                errors.append(
                    f"{artifact_id}: cached content differs from trusted digest"
                )
            if record.resolved_version != authority["resolved_version"]:
                errors.append(
                    f"{artifact_id}: resolved version differs from authoritative policy"
                )
            if record.source_url != authority["source_url"]:
                errors.append(
                    f"{artifact_id}: cached URL differs from authoritative source"
                )
            if dynamic and record.requested_version != value:
                errors.append(
                    f"{artifact_id}: resolution did not record unresolved input"
                )
            elif not dynamic and record.requested_version != value:
                errors.append(f"{artifact_id}: requested version differs from {version_key}")
            elif (
                not dynamic
                and raw.get("kind") != "git"
                and record.resolved_version != value
            ):
                errors.append(f"{artifact_id}: resolved version differs from {version_key}")

    for model in models:
        artifact_id = f"model:{model['name']}"
        if not include_models or (
            artifact_ids is not None and artifact_id not in artifact_ids
        ):
            continue
        revision = model["revision"]
        if reproducible:
            record = records.get(artifact_id)
            if revision == "dynamic":
                errors.append(
                    f"{artifact_id}: reproducible mode needs a trusted revision in models.manifest"
                )
            if record is None:
                errors.append(f"{artifact_id}: reproducible mode needs a resolved artifact")
            elif record.trust != "policy-bound":
                errors.append(
                    f"{artifact_id}: untrusted acquisition evidence is not reproducible"
                )
            elif not COMMIT_PATTERN.fullmatch(record.resolved_version):
                errors.append(f"{artifact_id}: resolved model revision is not immutable")
            elif revision != "dynamic" and record.requested_version != revision:
                errors.append(f"{artifact_id}: requested revision differs from manifest")
            elif revision != "dynamic" and record.resolved_version != revision:
                errors.append(f"{artifact_id}: resolved revision differs from manifest")
            elif artifact_manifest is not None:
                errors.extend(
                    validate_model_snapshot(
                        root,
                        cache_root or artifact_manifest.parent,
                        record,
                    )
                )
    return sorted(set(errors))


def _choose_generated_target(
    canonical: Path,
    fallback: Path,
    marker: bytes,
) -> Path:
    # Canonical files belong to the user, including when they do not exist yet.
    # Oracle always owns a named sidecar and consumers select that path explicitly.
    if fallback.exists() and (
        not fallback.is_file() or not fallback.read_bytes().startswith(marker)
    ):
        raise LifecycleError(
            f"refusing to overwrite unrelated generated-config fallback: {fallback}"
        )
    return fallback


def _guard_managed_target(root: Path, home: Path, target: Path) -> None:
    absolute = target.absolute()
    for base in (root, home):
        try:
            relative = absolute.relative_to(base)
        except ValueError:
            continue
        cursor = base
        for part in relative.parts[:-1]:
            cursor = cursor / part
            if _is_reparse_point(cursor):
                raise LifecycleError(
                    f"refusing generated config under symbolic-link parent: {cursor}"
                )
        if _is_reparse_point(absolute):
            raise LifecycleError(
                f"refusing to replace generated-config symbolic link: {absolute}"
            )
        return
    raise LifecycleError(f"generated config target is outside managed roots: {target}")


def _load_json_template(path: Path, default: Mapping[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return json.loads(json.dumps(default))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"configuration template is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise LifecycleError(f"configuration template must be an object: {path}")
    return payload


def _detect_model_ids(
    root: Path, models: Sequence[Mapping[str, str]]
) -> list[str]:
    cache_root = root / "incoming" / "dependency-cache"
    detected: list[str] = []
    for model in models:
        try:
            validated_model_paths(root, cache_root, model["name"])
        except LifecycleError:
            continue
        else:
            detected.append(model["name"])
    return detected


def _model_file_size(root: Path, model_name: str) -> int:
    total = 0
    for path in (root / "models" / model_name).rglob("*.gguf"):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _pick_tier(
    candidates: Sequence[Mapping[str, str]],
    profile: set[str],
    slots: Sequence[str],
) -> str:
    for in_profile in (True, False):
        for slot in slots:
            for model in candidates:
                if model["slot"] != slot:
                    continue
                selected = not profile or model["name"] in profile
                if selected == in_profile:
                    return model["name"]
    return candidates[0]["name"] if candidates else ""


def sync_model_configs(root: Path, home: Path) -> list[Path]:
    """Generate machine configs without changing tracked source templates."""

    root = _guard_state_root(root)
    home = home.resolve()
    manifest_path = root / "serving" / "models.manifest"
    try:
        models, errors = _parse_models_text(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"model manifest is unreadable: {exc}") from exc
    if errors:
        raise LifecycleError("; ".join(errors))
    ids = _detect_model_ids(root, models)
    if not ids:
        raise LifecycleError(
            "no policy-bound model snapshot is valid; generated engine "
            "configuration is unavailable"
        )
    by_name = {model["name"]: model for model in models}
    detected = [by_name[name] for name in ids if name in by_name]
    chat = [model for model in detected if model["slot"] != "embed"]
    if not chat:
        raise LifecycleError("detected models contain no chat-capable model")
    profile_path = root / "serving" / "models.profile"
    profile = set()
    if profile_path.is_file():
        profile = {
            line.strip()
            for line in profile_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    sonnet = _pick_tier(chat, profile, ("fast", "big"))
    opus = _pick_tier(chat, profile, ("big", "fast"))
    fast = [
        model
        for model in chat
        if model["slot"] == "fast"
        and str(model["context"]).isdigit()
        and int(model["context"]) >= 32768
    ]
    fast = sorted(
        fast,
        key=lambda model: (
            _model_file_size(root, model["name"]) or sys.maxsize,
            model["name"],
        ),
    )
    haiku = fast[0]["name"] if fast else sonnet
    anchor = sonnet
    ordered = [by_name[anchor], *[model for model in detected if model["name"] != anchor]]

    tiers_text = (
        f"OPUS_MODEL={opus}\n"
        f"SONNET_MODEL={sonnet}\n"
        f"HAIKU_MODEL={haiku}\n"
    )
    tiers_path = root / "serving" / "tiers.env"

    claude_template = root / "engines" / "claude-code" / "home" / "settings.json"
    claude = _load_json_template(claude_template, {"env": {}})
    environment = claude.get("env")
    if not isinstance(environment, dict):
        environment = {}
        claude["env"] = environment
    environment.update(
        {
            "ANTHROPIC_MODEL": sonnet,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": opus,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku,
            "ANTHROPIC_SMALL_FAST_MODEL": haiku,
        }
    )
    claude["model"] = sonnet
    claude_path = root / "state" / "generated" / "claude-code" / "settings.json"

    opencode_template = (
        root / "engines" / "opencode" / "xdg" / "opencode" / "opencode.json"
    )
    opencode = _load_json_template(
        opencode_template, {"provider": {"oracle": {"models": {}}}}
    )
    providers = opencode.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise LifecycleError("OpenCode template provider must be an object")
    oracle = providers.setdefault("oracle", {})
    if not isinstance(oracle, dict):
        raise LifecycleError("OpenCode template oracle provider must be an object")
    model_map: dict[str, Any] = {}
    for model in chat:
        context = (
            int(model["context"]) if str(model["context"]).isdigit() else 32768
        )
        output = min(max(context // 2, 8192), 65536)
        model_map[model["name"]] = {
            "name": f"{model['name']} (local)",
            "tool_call": True,
            "limit": {"context": context, "output": output},
        }
        if "thinking" in model["name"]:
            model_map[model["name"]]["reasoning"] = True
    oracle["models"] = model_map
    opencode["model"] = f"oracle/{sonnet}"
    opencode["small_model"] = f"oracle/{haiku}"
    opencode_path = root / "state" / "generated" / "opencode" / "opencode.json"

    continue_marker = b"# GENERATED by SentiVue Oracle lifecycle\n"
    continue_path = root / "state" / "generated" / "continue" / "config.yaml"
    continue_lines = [
        continue_marker.decode("ascii").rstrip("\n"),
        "name: SentiVue Oracle",
        "version: 1.0.0",
        "models:",
    ]
    for model in ordered:
        slot = model["slot"]
        roles = (
            "[embed]"
            if slot == "embed"
            else "[chat, edit, apply, autocomplete]"
            if slot == "fast"
            else "[chat, edit, apply]"
        )
        continue_lines.extend(
            [
                f"  - name: {model['name']} (local)",
                "    provider: openai",
                f"    model: {model['name']}",
                "    apiBase: http://127.0.0.1:9099/v1",
                "    apiKey: oracle-local",
                f"    roles: {roles}",
            ]
        )
        if slot != "embed":
            continue_lines.extend(
                [
                    "    capabilities: [tool_use]",
                    "    defaultCompletionOptions:",
                    f"      contextLength: {model['context']}",
                ]
            )
    continue_text = "\n".join(continue_lines) + "\n"

    kilo_marker = b"// GENERATED by SentiVue Oracle lifecycle\n"
    kilo_path = root / "state" / "generated" / "kilo" / "kilo.jsonc"
    kilo_models = {
        name: value for name, value in model_map.items()
    }
    kilo_payload = {
        "model": f"openai-compatible/{anchor}",
        "share": "disabled",
        "enabled_providers": ["openai-compatible"],
        "instructions": [
            str(root / "engines" / "shared" / "IDE-AGENT.md"),
            str(root / "engines" / "shared" / "CONVENTIONS.md"),
            str(root / "engines" / "shared" / "AUTONOMY.md"),
        ],
        "provider": {
            "openai-compatible": {
                "options": {
                    "apiKey": "oracle-local",
                    "baseURL": "http://127.0.0.1:9099/v1",
                },
                "models": kilo_models,
            }
        },
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
        "experimental": {"openTelemetry": False},
    }
    kilo_text = kilo_marker.decode("ascii") + json.dumps(
        kilo_payload, indent=2, sort_keys=True
    ) + "\n"

    writes = {
        tiers_path: tiers_text,
        claude_path: _json_bytes(claude).decode("utf-8"),
        opencode_path: _json_bytes(opencode).decode("utf-8"),
        continue_path: continue_text,
        kilo_path: kilo_text,
    }
    for path in writes:
        _guard_managed_target(root, home, path)
    if not _state_path(root).is_file():
        initialize_install_state(root, home)
    for path, text in writes.items():
        atomic_write_text(path, text)
        register_owned_path(root, home, path)
    return sorted(writes, key=lambda path: str(path))


def _guard_state_root(root: Path) -> Path:
    root = Path(os.path.abspath(os.fspath(root)))
    for candidate in [*reversed(root.parents), root]:
        if candidate == Path(candidate.anchor):
            continue
        if _is_reparse_point(candidate):
            raise LifecycleError(
                f"install state root has a reparse ancestor: {candidate}"
            )
    return root


def _state_path(root: Path) -> Path:
    root = _guard_state_root(root)
    state_directory = root / STATE_DIRECTORY
    state_file = state_directory / STATE_FILE
    if _is_reparse_point(state_directory):
        raise LifecycleError(
            f"install state directory is a reparse point: {state_directory}"
        )
    if state_directory.exists() and not state_directory.is_dir():
        raise LifecycleError(
            f"install state directory is not a directory: {state_directory}"
        )
    if _is_reparse_point(state_file):
        raise LifecycleError(f"install state file is a reparse point: {state_file}")
    return state_file


def _input_digest(root: Path, source_revision: str) -> str:
    digest = hashlib.sha256()
    inputs = (
        "VERSIONS.lock",
        "serving/models.manifest",
        "serving/model-authorities.json",
        "verification/policy.json",
        DEPENDENCY_AUTHORITY_FILE,
        "env/uv.lock",
        "ARTIFACTS.json",
        "incoming/dependency-cache/manifest.json",
    )
    for relative in inputs:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        path = root / relative
        data = path.read_bytes() if path.is_file() else b"<missing>"
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    revision = source_revision.encode("ascii", errors="strict")
    digest.update(len(revision).to_bytes(4, "big"))
    digest.update(revision)
    return digest.hexdigest()


def _read_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LifecycleError("install state is missing; initialize it first") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"install state is invalid: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise LifecycleError("install state has an unsupported schema")
    if not isinstance(state.get("owned_paths"), list):
        raise LifecycleError("install state owned_paths must be a list")
    if not isinstance(state.get("phases"), dict):
        raise LifecycleError("install state phases must be an object")
    if "owned_services" not in state:
        state["owned_services"] = []
    if "pending_phase" not in state:
        state["pending_phase"] = None
    if not isinstance(state.get("owned_services"), list):
        raise LifecycleError("install state owned_services must be a list")
    return state


def _write_state(root: Path, state: Mapping[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _state_path(root)
    atomic_write_bytes(path, _json_bytes(dict(state)))


def _current_revision(root: Path) -> str:
    completed = _run(["git", "rev-parse", "HEAD"], cwd=root)
    if completed.returncode == 0:
        revision = str(completed.stdout).strip()
        if COMMIT_PATTERN.fullmatch(revision):
            return revision
    provenance_path = root / "SOURCE-PROVENANCE.json"
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            if isinstance(provenance, dict):
                revision = str(provenance.get("source_revision", ""))
                if (
                    provenance.get("schema_version") == SCHEMA_VERSION
                    and COMMIT_PATTERN.fullmatch(revision)
                ):
                    return revision
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return "0" * 40


def initialize_install_state(
    root: Path,
    home: Path,
    *,
    source_revision: str | None = None,
) -> dict[str, Any]:
    """Initialize or upgrade state without losing ownership history."""

    root = Path(os.path.abspath(os.fspath(root)))
    home = home.resolve()
    _guard_state_root(root)
    root.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    _guard_state_root(root)
    revision = source_revision or _current_revision(root)
    if not COMMIT_PATTERN.fullmatch(revision):
        raise LifecycleError("install source revision must be a 40-character commit")
    legacy = root / STATE_DIRECTORY
    if _is_reparse_point(legacy):
        raise LifecycleError("legacy install state must not be a reparse point")
    if legacy.is_file():
        try:
            legacy.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LifecycleError("legacy install state is unreadable") from exc
        legacy.unlink()
    path = _state_path(root)
    if path.is_file():
        previous = _read_state(root)
        if previous.get("installation_root") != str(root):
            raise LifecycleError("install state belongs to a different installation root")
        if previous.get("home_root") != str(home):
            raise LifecycleError("install state belongs to a different home root")
        owned_paths = previous["owned_paths"]
        owned_services = previous["owned_services"]
        phases = previous["phases"]
        pending_phase = previous.get("pending_phase")
        created_at = previous.get("created_at")
    else:
        owned_paths = []
        owned_services = []
        phases = {}
        pending_phase = None
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state = {
        "schema_version": SCHEMA_VERSION,
        "installation_root": str(root),
        "home_root": str(home),
        "source_revision": revision,
        "input_sha256": _input_digest(root, revision),
        "created_at": created_at,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phases": phases,
        "pending_phase": pending_phase,
        "owned_paths": owned_paths,
        "owned_services": owned_services,
    }
    _write_state(root, state)
    return state


def phase_is_current(root: Path, phase: str) -> bool:
    root = _guard_state_root(root)
    state = _read_state(root)
    phase_record = state["phases"].get(phase)
    if isinstance(phase_record, str):
        phase_input = phase_record
        phase_paths = state["owned_paths"]
    elif isinstance(phase_record, dict):
        phase_input = phase_record.get("input_sha256")
        phase_paths = phase_record.get("owned_paths")
        if not isinstance(phase_paths, list):
            return False
    else:
        return False
    if phase_input != state["input_sha256"]:
        return False
    home = Path(str(state.get("home_root", "")))
    try:
        for raw in phase_paths:
            if not isinstance(raw, dict):
                return False
            _scope, _relative, kind, digest, target = _owned_target(
                root, home, raw
            )
            if kind == "directory":
                if not target.is_dir() or _is_reparse_point(target):
                    return False
            elif kind == "symlink":
                if not _is_reparse_point(target):
                    return False
                if _owned_fingerprint(target, kind) != digest:
                    return False
            elif not target.is_file() or _owned_fingerprint(target, kind) != digest:
                return False
    except (LifecycleError, OSError):
        return False
    return True


def _validate_phase_name(phase: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", phase):
        raise LifecycleError("install phase has an invalid name")


def begin_install_phase(root: Path, phase: str) -> None:
    """Associate subsequently registered ownership with one install phase."""

    _validate_phase_name(phase)
    root = _guard_state_root(root)
    state = _read_state(root)
    state["pending_phase"] = phase
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _write_state(root, state)


def mark_install_phase(root: Path, phase: str) -> None:
    _validate_phase_name(phase)
    root = _guard_state_root(root)
    state = _read_state(root)
    phase_paths = [
        item
        for item in state["owned_paths"]
        if isinstance(item, dict) and item.get("phase") == phase
    ]
    if not phase_paths and state.get("pending_phase") != phase:
        # Direct API callers predating begin-phase retain the original behavior.
        phase_paths = state["owned_paths"]
    state["phases"][phase] = {
        "input_sha256": state["input_sha256"],
        "owned_paths": json.loads(json.dumps(phase_paths)),
    }
    if state.get("pending_phase") == phase:
        state["pending_phase"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _write_state(root, state)


def _path_scope(root: Path, home: Path, candidate: Path) -> tuple[str, str]:
    candidate = candidate.absolute()
    for scope, base in (("install", root), ("home", home)):
        try:
            relative_path = candidate.relative_to(base)
        except ValueError:
            continue
        relative = relative_path.as_posix()
        validate_relative_path(relative)
        return scope, relative
    raise LifecycleError(f"owned path is outside install/home roots: {candidate}")


def _owned_fingerprint(path: Path, kind: str) -> str | None:
    if kind == "file":
        return _sha256_file(path)
    if kind == "symlink":
        return _sha256_bytes(os.readlink(path).encode("utf-8"))
    return None


def _owned_entry(root: Path, home: Path, candidate: Path) -> dict[str, Any]:
    candidate = candidate.absolute()
    if _is_reparse_point(candidate):
        kind = "symlink"
    elif candidate.is_file():
        kind = "file"
    elif candidate.is_dir():
        kind = "directory"
    else:
        raise LifecycleError(f"owned path does not exist: {candidate}")
    scope, relative = _path_scope(root, home, candidate)
    base = root if scope == "install" else home
    _managed_target(base, relative, allow_final_reparse=kind == "symlink")
    return {
        "scope": scope,
        "path": relative,
        "kind": kind,
        "sha256": _owned_fingerprint(candidate, kind),
    }


def register_owned_path(root: Path, home: Path, path: Path) -> dict[str, Any]:
    """Record a path that Oracle actually created, with a content fingerprint."""

    root = _guard_state_root(root)
    home = home.resolve()
    entry = _owned_entry(root, home, path)
    scope = entry["scope"]
    relative = entry["path"]
    state = _read_state(root)
    previous = next(
        (
            item
            for item in state["owned_paths"]
            if isinstance(item, dict)
            and item.get("scope") == scope
            and item.get("path") == relative
        ),
        None,
    )
    owner_phase = state.get("pending_phase")
    if not isinstance(owner_phase, str) and isinstance(previous, dict):
        owner_phase = previous.get("phase")
    if isinstance(owner_phase, str):
        entry["phase"] = owner_phase
    state["owned_paths"] = [
        item
        for item in state["owned_paths"]
        if not (
            isinstance(item, dict)
            and item.get("scope") == scope
            and item.get("path") == relative
        )
    ]
    state["owned_paths"].append(entry)
    state["owned_paths"] = sorted(
        state["owned_paths"],
        key=lambda item: (str(item.get("scope")), str(item.get("path"))),
    )
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _write_state(root, state)
    return entry


def register_owned_tree(
    root: Path,
    home: Path,
    tree: Path,
) -> list[dict[str, Any]]:
    """Replace ownership records for a tree with every path currently present."""

    root = _guard_state_root(root)
    home = home.resolve()
    tree = tree.absolute()
    if _is_reparse_point(tree) or not tree.is_dir():
        raise LifecycleError(f"owned tree is missing or unsafe: {tree}")
    paths = [tree]
    for directory, directory_names, file_names in os.walk(tree, followlinks=False):
        current = Path(directory)
        for name in list(directory_names):
            child = current / name
            paths.append(child)
            if _is_reparse_point(child):
                directory_names.remove(name)
        paths.extend(current / name for name in file_names)
    entries = [_owned_entry(root, home, path) for path in paths]
    tree_scope, tree_relative = _path_scope(root, home, tree)
    prefix = f"{tree_relative}/"
    state = _read_state(root)
    previous_phases = {
        (str(item.get("scope")), str(item.get("path"))): item.get("phase")
        for item in state["owned_paths"]
        if isinstance(item, dict) and isinstance(item.get("phase"), str)
    }
    pending_phase = state.get("pending_phase")
    for entry in entries:
        owner_phase = (
            pending_phase
            if isinstance(pending_phase, str)
            else previous_phases.get((str(entry["scope"]), str(entry["path"])))
        )
        if isinstance(owner_phase, str):
            entry["phase"] = owner_phase
    state["owned_paths"] = [
        item
        for item in state["owned_paths"]
        if not (
            isinstance(item, dict)
            and item.get("scope") == tree_scope
            and (
                item.get("path") == tree_relative
                or str(item.get("path", "")).startswith(prefix)
            )
        )
    ]
    state["owned_paths"].extend(entries)
    state["owned_paths"] = sorted(
        state["owned_paths"],
        key=lambda item: (str(item.get("scope")), str(item.get("path"))),
    )
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _write_state(root, state)
    return entries


def register_model_ownership(
    root: Path,
    home: Path,
    cache_root: Path,
    model_name: str,
) -> list[dict[str, Any]]:
    """Own only authority-selected model shard files, never the models tree."""

    entries = []
    for path in validated_model_paths(root, cache_root, model_name):
        entries.append(register_owned_path(root, home, path))
    return entries


def register_owned_service(
    root: Path,
    *,
    kind: str,
    identifier: str,
) -> dict[str, str]:
    """Record one narrowly validated service that Oracle created."""

    _validate_owned_service({"kind": kind, "identifier": identifier})
    root = _guard_state_root(root)
    state = _read_state(root)
    entry = {"kind": kind, "identifier": identifier}
    state["owned_services"] = [
        item
        for item in state["owned_services"]
        if not (
            isinstance(item, dict)
            and item.get("kind") == kind
            and item.get("identifier") == identifier
        )
    ]
    state["owned_services"].append(entry)
    state["owned_services"] = sorted(
        state["owned_services"],
        key=lambda item: (str(item.get("kind")), str(item.get("identifier"))),
    )
    state["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _write_state(root, state)
    return entry


def _validate_owned_service(raw: Mapping[str, Any]) -> dict[str, str]:
    kind = raw.get("kind")
    identifier = raw.get("identifier")
    if not isinstance(identifier, str):
        raise LifecycleError("unsafe owned service in install state")
    if kind == "launchd-user" and re.fullmatch(
        r"com\.sentivue\.[a-z0-9.-]+", identifier
    ):
        return {"kind": kind, "identifier": identifier}
    if kind == "windows-pid-file" and identifier == "state/llama-swap.pid":
        return {"kind": kind, "identifier": identifier}
    if (
        kind == "windows-scheduled-task"
        and identifier == "SentiVueOracleServing"
    ):
        return {"kind": kind, "identifier": identifier}
    raise LifecycleError("unsafe owned service identifier in install state")


def _stop_owned_windows_process(root: Path, identifier: str) -> None:
    if os.name != "nt":
        raise LifecycleError("cannot safely stop an owned Windows process here")
    pid_path = _managed_target(root, identifier, allow_final_reparse=False)
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise LifecycleError("owned Windows PID file is invalid") from exc
    if pid <= 0 or pid > 0xFFFFFFFF:
        raise LifecycleError("owned Windows PID is outside the safe range")

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x1001, False, pid)
    if not process:
        error = ctypes.get_last_error()
        if error == 87:
            return
        raise LifecycleError(f"cannot inspect owned Windows process {pid}: {error}")
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(capacity)
        ):
            raise LifecycleError(
                f"cannot identify owned Windows process {pid}: {ctypes.get_last_error()}"
            )
        expected = root / ".tools" / "win" / "llama-swap.exe"
        actual = Path(buffer.value)
        if os.path.normcase(os.path.realpath(actual)) != os.path.normcase(
            os.path.realpath(expected)
        ):
            raise LifecycleError(
                f"refusing to stop PID {pid}: executable is not Oracle llama-swap"
            )
        if not kernel32.TerminateProcess(process, 0):
            raise LifecycleError(
                f"could not stop owned Windows process {pid}: {ctypes.get_last_error()}"
            )
        kernel32.WaitForSingleObject(process, 5000)
    finally:
        kernel32.CloseHandle(process)


def _stop_owned_service(service: Mapping[str, str], root: Path | None = None) -> None:
    if service["kind"] == "windows-pid-file":
        if root is None:
            raise LifecycleError("Windows process stop requires an installation root")
        _stop_owned_windows_process(root, service["identifier"])
        return
    if service["kind"] == "windows-scheduled-task":
        if os.name != "nt":
            raise LifecycleError("cannot safely remove an owned Scheduled Task here")
        executable = shutil.which("schtasks.exe") or shutil.which("schtasks")
        if not executable:
            raise LifecycleError("schtasks is required to remove the owned service")
        identifier = service["identifier"]
        present = _run([executable, "/Query", "/TN", identifier])
        if present.returncode != 0:
            return
        _run([executable, "/End", "/TN", identifier])
        deleted = _run([executable, "/Delete", "/TN", identifier, "/F"])
        if deleted.returncode != 0:
            detail = deleted.stderr or deleted.stdout or "schtasks delete failed"
            raise LifecycleError(
                f"could not remove owned Scheduled Task {identifier}: {detail.strip()}"
            )
        return
    if service["kind"] != "launchd-user":
        raise LifecycleError("unsafe owned service identifier in install state")
    launchctl = shutil.which("launchctl")
    if not launchctl or not hasattr(os, "getuid"):
        raise LifecycleError("cannot safely stop owned launchd service on this platform")
    target = f"gui/{os.getuid()}/{service['identifier']}"
    present = _run([launchctl, "print", target])
    if present.returncode != 0:
        return
    stopped = _run([launchctl, "bootout", target])
    if stopped.returncode != 0:
        detail = stopped.stderr or stopped.stdout or "launchctl bootout failed"
        raise LifecycleError(
            f"could not stop owned service {service['identifier']}: {detail.strip()}"
        )


def _is_reparse_point(path: Path) -> bool:
    """Recognize POSIX links and Windows junction/reparse entries without following."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _managed_target(
    base: Path,
    relative: str,
    *,
    allow_final_reparse: bool,
) -> Path:
    """Join beneath a trusted real root and reject every traversed reparse entry."""

    base = Path(os.path.realpath(base))
    parts = PurePosixPath(validate_relative_path(relative)).parts
    target = base.joinpath(*parts)
    try:
        if os.path.commonpath((str(base), str(target))) != str(base):
            raise LifecycleError("managed path escapes its trusted real root")
    except ValueError as exc:
        raise LifecycleError("managed path crosses filesystem roots") from exc
    current = base
    for index, part in enumerate(parts):
        current = current / part
        is_final = index == len(parts) - 1
        if _is_reparse_point(current):
            if is_final and allow_final_reparse:
                continue
            role = "target" if is_final else "ancestor"
            raise LifecycleError(
                f"managed path has a symlink or reparse {role}: {current}"
            )
        if not is_final and current.exists() and not current.is_dir():
            raise LifecycleError(f"managed path ancestor changed type: {current}")
        if current.exists():
            resolved = Path(os.path.realpath(current))
            try:
                contained = os.path.commonpath((str(base), str(resolved))) == str(base)
            except ValueError:
                contained = False
            if not contained:
                raise LifecycleError(
                    f"managed path resolves outside its trusted real root: {current}"
                )
    return target


def _owned_target(
    root: Path, home: Path, raw: Mapping[str, Any]
) -> tuple[str, str, str, str | None, Path]:
    scope = raw.get("scope")
    relative = raw.get("path")
    kind = raw.get("kind")
    digest = raw.get("sha256")
    if scope not in {"install", "home"}:
        raise LifecycleError("unsafe ownership scope in install state")
    if not isinstance(relative, str):
        raise LifecycleError("unsafe ownership path in install state")
    try:
        validate_relative_path(relative)
    except LifecycleError as exc:
        raise LifecycleError(f"unsafe ownership path in install state: {exc}") from exc
    if kind not in {"file", "symlink", "directory"}:
        raise LifecycleError("unsafe ownership kind in install state")
    if kind in {"file", "symlink"} and not (
        isinstance(digest, str) and SHA256_PATTERN.fullmatch(digest)
    ):
        raise LifecycleError("unsafe ownership fingerprint in install state")
    base = root if scope == "install" else home
    target = _managed_target(
        base, relative, allow_final_reparse=kind == "symlink"
    )
    return scope, relative, kind, digest if isinstance(digest, str) else None, target


def _remove_owned(
    target: Path,
    *,
    kind: str,
    expected_digest: str | None,
    apply: bool,
) -> tuple[str, str]:
    if not target.exists() and not target.is_symlink():
        return "missing", "path is already absent"
    if kind == "directory":
        if not target.is_dir() or _is_reparse_point(target):
            return "preserve-modified", "owned directory changed type"
        try:
            next(target.iterdir())
        except StopIteration:
            if apply:
                target.rmdir()
            return "remove", "owned directory is empty"
        return "preserve-nonempty", "owned directory contains unowned entries"
    actual_kind = (
        "symlink"
        if _is_reparse_point(target)
        else "file"
        if target.is_file()
        else ""
    )
    if actual_kind != kind:
        return "preserve-modified", "owned path changed type"
    actual_digest = _owned_fingerprint(target, kind)
    if actual_digest != expected_digest:
        return "preserve-modified", "content changed after Oracle wrote it"
    if apply:
        target.unlink()
    return "remove", "content matches ownership record"


def uninstall(
    root: Path,
    home: Path,
    *,
    apply: bool = False,
    purge: bool = False,
    confirm_purge: bool = False,
    service_stopper: Callable[[dict[str, str]], None] | None = None,
) -> UninstallPlan:
    """Plan or apply ownership-scoped removal.  Dry-run is the default."""

    root = _guard_state_root(root)
    home = home.resolve()
    if purge and not confirm_purge:
        raise LifecycleError("purge requires the separate confirmation flag")
    state = _read_state(root)
    if state.get("installation_root") != str(root):
        raise LifecycleError("install state root does not match this installation")
    if state.get("home_root") != str(home):
        raise LifecycleError("install state home does not match this user")
    entries: list[UninstallEntry] = []
    retained: list[dict[str, Any]] = []
    services = []
    for raw_service in state["owned_services"]:
        if not isinstance(raw_service, dict):
            raise LifecycleError("unsafe non-object service entry in install state")
        services.append(_validate_owned_service(raw_service))
    raw_owned = state["owned_paths"]
    validated = []
    for raw in raw_owned:
        if not isinstance(raw, dict):
            raise LifecycleError("unsafe non-object ownership entry in install state")
        validated.append((raw, _owned_target(root, home, raw)))
    validated.sort(
        key=lambda item: len(PurePosixPath(item[1][1]).parts),
        reverse=True,
    )
    for service in services:
        entries.append(
            UninstallEntry(
                "service",
                service["identifier"],
                "stop",
                "owned service is stopped before file removal",
            )
        )
        if apply:
            if service_stopper is not None:
                service_stopper(service)
            else:
                _stop_owned_service(service, root)
    for raw, (scope, relative, kind, digest, target) in validated:
        base = root if scope == "install" else home
        target = _managed_target(
            base, relative, allow_final_reparse=kind == "symlink"
        )
        action, reason = _remove_owned(
            target,
            kind=kind,
            expected_digest=digest,
            apply=apply,
        )
        entries.append(UninstallEntry(scope, relative, action, reason))
        if action in {"preserve-modified", "preserve-nonempty"}:
            retained.append(raw)

    if purge:
        for relative in PURGE_ROOTS:
            validate_relative_path(relative)
            target = _managed_target(
                root, relative, allow_final_reparse=True
            )
            if target.exists() or target.is_symlink():
                entries.append(
                    UninstallEntry(
                        "install",
                        relative,
                        "purge",
                        "explicitly confirmed Oracle runtime root",
                    )
                )
                if apply:
                    target = _managed_target(
                        root, relative, allow_final_reparse=True
                    )
                    if _is_reparse_point(target):
                        if target.is_dir():
                            target.rmdir()
                        else:
                            target.unlink()
                    elif target.is_file():
                        target.unlink()
                    else:
                        shutil.rmtree(target)

    if apply and not purge:
        state["owned_services"] = []
        state["owned_paths"] = retained
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        _write_state(root, state)
    return UninstallPlan(applied=apply, purge=purge, entries=entries)


def _print_uninstall(plan: UninstallPlan) -> None:
    mode = "APPLY" if plan.applied else "DRY-RUN"
    print(f"uninstall {mode}: ownership-scoped")
    for entry in plan.entries:
        print(f"{entry.action:20} {entry.scope}:{entry.path} - {entry.reason}")
    if not plan.applied:
        print("No files changed. Re-run with --apply to remove eligible owned paths.")


def _command_package(args: argparse.Namespace) -> int:
    bundle = build_source_archives(
        args.root, args.revision, args.output, args.version
    )
    verify_release_bundle(bundle.output_dir)
    print(bundle.output_dir)
    return 0


def _command_release(args: argparse.Namespace) -> int:
    if args.publish:
        bundle = publish_release(
            args.root, args.version, args.output, args.revision
        )
    else:
        bundle = preflight_release(
            args.root, args.version, args.output, args.revision
        )
    print(bundle.output_dir)
    return 0


def _command_validate_dependencies(args: argparse.Namespace) -> int:
    errors = validate_dependency_inputs(
        args.root,
        artifact_manifest=args.manifest,
        cache_root=args.cache,
        reproducible=args.reproducible,
        include_optional=args.include_optional,
    )
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("dependency inputs validated")
    return 0


def _command_record_artifact(args: argparse.Namespace) -> int:
    record = record_cached_artifact(
        args.cache,
        artifact_id=args.artifact_id,
        source_file=args.file,
        source_url=args.url,
        requested_version=args.requested_version,
        resolved_version=args.resolved_version,
    )
    print(json.dumps(record.to_payload(), sort_keys=True))
    return 0


def _command_export_artifact(args: argparse.Namespace) -> int:
    record = export_artifact(
        args.cache,
        artifact_id=args.artifact_id,
        source_url=args.url,
        requested_version=args.requested_version,
        resolved_version=args.resolved_version,
        expected_sha256=args.expected_sha256,
        policy_root=args.root,
        trusted=args.trusted,
    )
    print(json.dumps(record.to_payload(), sort_keys=True))
    return 0


def _command_import_artifact(args: argparse.Namespace) -> int:
    record = import_artifact(
        args.root,
        args.cache,
        artifact_id=args.artifact_id,
        source_file=args.file,
        source_url=args.url,
        requested_version=args.requested_version,
        resolved_version=args.resolved_version,
    )
    print(json.dumps(record.to_payload(), sort_keys=True))
    return 0


def _command_promote_authority(args: argparse.Namespace) -> int:
    authority = promote_dependency_authority(
        args.root,
        artifact_id=args.artifact_id,
        authority_file=args.authority,
    )
    print(json.dumps(authority, sort_keys=True))
    return 0


def _command_artifact_path(args: argparse.Namespace) -> int:
    path = resolve_cached_artifact(
        args.manifest,
        args.cache,
        args.artifact_id,
        expected_version=args.expected_version,
        expected_requested_version=args.expected_requested_version,
        policy_root=args.root,
        require_policy_bound=args.reproducible,
    )
    print(path)
    return 0


def _command_record_model(args: argparse.Namespace) -> int:
    record = record_model_snapshot(
        args.root,
        args.cache,
        models_root=args.models_root,
        model_name=args.model_name,
        repository=args.repository,
        requested_revision=args.requested_revision,
        resolved_revision=args.resolved_revision,
    )
    print(json.dumps(record.to_payload(), sort_keys=True))
    return 0


def _command_import_model(args: argparse.Namespace) -> int:
    record = import_model_snapshot(
        args.root,
        args.cache,
        models_root=args.models_root,
        model_name=args.model_name,
        authority_file=args.authority,
    )
    print(json.dumps(record.to_payload(), sort_keys=True))
    return 0


def _command_model_path(args: argparse.Namespace) -> int:
    print(preferred_model_path(args.root, args.cache, args.model_name))
    return 0


def _command_install_source(args: argparse.Namespace) -> int:
    path = install_source_archive(
        args.root,
        args.manifest,
        args.cache,
        args.artifact_id,
        args.destination,
        trusted_root=args.trusted_root,
        expected_version=args.expected_version,
        expected_requested_version=args.expected_requested_version,
    )
    print(path)
    return 0


def _command_preflight_source(args: argparse.Namespace) -> int:
    path = preflight_source_install(
        args.root,
        args.manifest,
        args.cache,
        args.artifact_id,
        args.destination,
        trusted_root=args.trusted_root,
        expected_version=args.expected_version,
        expected_requested_version=args.expected_requested_version,
    )
    print(path)
    return 0


def _command_validate_source(args: argparse.Namespace) -> int:
    errors = validate_source_install(
        args.root,
        args.manifest,
        args.cache,
        args.artifact_id,
        args.destination,
        trusted_root=args.trusted_root,
        expected_version=args.expected_version,
        expected_requested_version=args.expected_requested_version,
    )
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("source install validated")
    return 0


def _command_state(args: argparse.Namespace) -> int:
    if args.state_action == "init":
        state = initialize_install_state(
            args.root, args.home, source_revision=args.revision
        )
        print(state["input_sha256"])
        return 0
    if args.state_action == "phase-current":
        if not args.phase:
            raise LifecycleError("state phase-current requires --phase")
        return 0 if phase_is_current(args.root, args.phase) else 1
    if args.state_action == "begin-phase":
        if not args.phase:
            raise LifecycleError("state begin-phase requires --phase")
        begin_install_phase(args.root, args.phase)
        return 0
    if args.state_action == "mark-phase":
        if not args.phase:
            raise LifecycleError("state mark-phase requires --phase")
        mark_install_phase(args.root, args.phase)
        return 0
    if args.state_action == "own":
        if args.path is None:
            raise LifecycleError("state own requires --path")
        register_owned_path(args.root, args.home, args.path)
        return 0
    if args.state_action == "own-tree":
        if args.path is None:
            raise LifecycleError("state own-tree requires --path")
        register_owned_tree(args.root, args.home, args.path)
        return 0
    if args.state_action == "own-service":
        if not args.service_kind or not args.identifier:
            raise LifecycleError(
                "state own-service requires --service-kind and --identifier"
            )
        register_owned_service(
            args.root, kind=args.service_kind, identifier=args.identifier
        )
        return 0
    if args.state_action == "own-model":
        if not args.model_name or args.cache is None:
            raise LifecycleError("state own-model requires --model-name and --cache")
        register_model_ownership(
            args.root, args.home, args.cache, args.model_name
        )
        return 0
    raise LifecycleError("unknown state action")


def _command_uninstall(args: argparse.Namespace) -> int:
    plan = uninstall(
        args.root,
        args.home,
        apply=args.apply,
        purge=args.purge,
        confirm_purge=args.confirm_purge,
    )
    _print_uninstall(plan)
    return 0


def _command_sync_config(args: argparse.Namespace) -> int:
    written = sync_model_configs(args.root, args.home)
    if not written:
        print("sync-config: no models detected; existing configs were untouched")
        return 0
    for path in written:
        print(path)
    return 0


def _command_quote_argument(args: argparse.Namespace) -> int:
    print(quote_command_argument(args.value, args.platform))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproducible SentiVue Oracle lifecycle operations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    package = subparsers.add_parser("package")
    package.add_argument("--root", type=Path, required=True)
    package.add_argument("--revision", default="HEAD")
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--version", required=True)
    package.set_defaults(handler=_command_package)

    release = subparsers.add_parser("release")
    release.add_argument("--root", type=Path, required=True)
    release.add_argument("--revision", default="HEAD")
    release.add_argument("--output", type=Path, required=True)
    release.add_argument("--version", required=True)
    release_mode = release.add_mutually_exclusive_group()
    release_mode.add_argument("--preflight-only", action="store_true")
    release_mode.add_argument("--publish", action="store_true")
    release.set_defaults(handler=_command_release)

    dependencies = subparsers.add_parser("validate-dependencies")
    dependencies.add_argument("--root", type=Path, required=True)
    dependencies.add_argument("--manifest", type=Path)
    dependencies.add_argument("--cache", type=Path)
    dependencies.add_argument("--reproducible", action="store_true")
    dependencies.add_argument("--include-optional", action="store_true")
    dependencies.set_defaults(handler=_command_validate_dependencies)

    record = subparsers.add_parser("record-artifact")
    record.add_argument("--cache", type=Path, required=True)
    record.add_argument("--artifact-id", required=True)
    record.add_argument("--file", type=Path, required=True)
    record.add_argument("--url", required=True)
    record.add_argument("--requested-version", required=True)
    record.add_argument("--resolved-version", required=True)
    record.set_defaults(handler=_command_record_artifact)

    export = subparsers.add_parser("export-artifact")
    export.add_argument("--cache", type=Path, required=True)
    export.add_argument("--artifact-id", required=True)
    export.add_argument("--url", required=True)
    export.add_argument("--requested-version", required=True)
    export.add_argument("--resolved-version", required=True)
    export.add_argument("--expected-sha256")
    export.add_argument("--root", type=Path)
    export.add_argument("--trusted", action="store_true")
    export.set_defaults(handler=_command_export_artifact)

    import_dependency = subparsers.add_parser("import-artifact")
    import_dependency.add_argument("--root", type=Path, required=True)
    import_dependency.add_argument("--cache", type=Path, required=True)
    import_dependency.add_argument("--artifact-id", required=True)
    import_dependency.add_argument("--file", type=Path, required=True)
    import_dependency.add_argument("--url", required=True)
    import_dependency.add_argument("--requested-version", required=True)
    import_dependency.add_argument("--resolved-version", required=True)
    import_dependency.set_defaults(handler=_command_import_artifact)

    promote_authority = subparsers.add_parser("promote-authority")
    promote_authority.add_argument("--root", type=Path, required=True)
    promote_authority.add_argument("--artifact-id", required=True)
    promote_authority.add_argument("--authority", type=Path, required=True)
    promote_authority.set_defaults(handler=_command_promote_authority)

    artifact_path = subparsers.add_parser("artifact-path")
    artifact_path.add_argument("--manifest", type=Path, required=True)
    artifact_path.add_argument("--cache", type=Path, required=True)
    artifact_path.add_argument("--artifact-id", required=True)
    artifact_path.add_argument("--expected-version")
    artifact_path.add_argument("--expected-requested-version")
    artifact_path.add_argument("--root", type=Path)
    artifact_path.add_argument("--reproducible", action="store_true")
    artifact_path.set_defaults(handler=_command_artifact_path)

    record_model = subparsers.add_parser("record-model")
    record_model.add_argument("--root", type=Path, required=True)
    record_model.add_argument("--cache", type=Path, required=True)
    record_model.add_argument("--models-root", type=Path)
    record_model.add_argument("--model-name", required=True)
    record_model.add_argument("--repository", required=True)
    record_model.add_argument("--requested-revision", required=True)
    record_model.add_argument("--resolved-revision", required=True)
    record_model.set_defaults(handler=_command_record_model)

    import_model = subparsers.add_parser("import-model")
    import_model.add_argument("--root", type=Path, required=True)
    import_model.add_argument("--cache", type=Path, required=True)
    import_model.add_argument("--models-root", type=Path)
    import_model.add_argument("--model-name", required=True)
    import_model.add_argument("--authority", type=Path, required=True)
    import_model.set_defaults(handler=_command_import_model)

    model_path = subparsers.add_parser("model-path")
    model_path.add_argument("--root", type=Path, required=True)
    model_path.add_argument("--cache", type=Path, required=True)
    model_path.add_argument("--model-name", required=True)
    model_path.set_defaults(handler=_command_model_path)

    for command_name, handler in (
        ("preflight-source", _command_preflight_source),
        ("install-source", _command_install_source),
        ("validate-source", _command_validate_source),
    ):
        source_command = subparsers.add_parser(command_name)
        source_command.add_argument("--root", type=Path, required=True)
        source_command.add_argument("--manifest", type=Path, required=True)
        source_command.add_argument("--cache", type=Path, required=True)
        source_command.add_argument("--artifact-id", required=True)
        source_command.add_argument("--destination", type=Path, required=True)
        source_command.add_argument("--trusted-root", type=Path, required=True)
        source_command.add_argument("--expected-version", required=True)
        source_command.add_argument("--expected-requested-version", required=True)
        source_command.set_defaults(handler=handler)

    state = subparsers.add_parser("state")
    state.add_argument(
        "state_action",
        choices=(
            "init",
            "phase-current",
            "begin-phase",
            "mark-phase",
            "own",
            "own-tree",
            "own-service",
            "own-model",
        ),
    )
    state.add_argument("--root", type=Path, required=True)
    state.add_argument("--home", type=Path, required=True)
    state.add_argument("--revision")
    state.add_argument("--phase")
    state.add_argument("--path", type=Path)
    state.add_argument("--service-kind")
    state.add_argument("--identifier")
    state.add_argument("--model-name")
    state.add_argument("--cache", type=Path)
    state.set_defaults(handler=_command_state)

    remove = subparsers.add_parser("uninstall")
    remove.add_argument("--root", type=Path, required=True)
    remove.add_argument("--home", type=Path, required=True)
    remove.add_argument("--apply", action="store_true")
    remove.add_argument("--purge", action="store_true")
    remove.add_argument("--confirm-purge", action="store_true")
    remove.set_defaults(handler=_command_uninstall)

    sync = subparsers.add_parser("sync-config")
    sync.add_argument("--root", type=Path, required=True)
    sync.add_argument("--home", type=Path, required=True)
    sync.set_defaults(handler=_command_sync_config)

    quote_argument = subparsers.add_parser("quote-argument")
    quote_argument.add_argument("--platform", choices=("posix", "windows"), required=True)
    quote_argument.add_argument("--value", required=True)
    quote_argument.set_defaults(handler=_command_quote_argument)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LifecycleError as exc:
        print(f"lifecycle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
