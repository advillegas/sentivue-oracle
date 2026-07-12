"""Read-only, cross-platform verification for SentiVue Oracle.

The verifier reads source and runtime configuration but writes only to a unique
directory under reports/verification. It intentionally uses only Python's
standard library so static verification remains available before optional
models or the project environment are installed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
PROVISIONAL = "PROVISIONAL"
VALID_STATES = (PASS, FAIL, SKIP, PROVISIONAL)
VERIFIER_VERSION = 1
MAX_SOURCE_BYTES = 50 * 1024 * 1024

REQUIRED_GITATTRIBUTES = (
    "* text=auto eol=lf\n"
    "*.png binary\n"
    "*.jpg binary\n"
    "*.jpeg binary\n"
    "*.gif binary\n"
    "*.ico binary\n"
    "*.pdf binary\n"
    "*.zip binary\n"
    "*.gz binary\n"
)
REQUIRED_EDITORCONFIG = (
    "root = true\n"
    "\n"
    "[*]\n"
    "charset = utf-8\n"
    "end_of_line = lf\n"
    "insert_final_newline = true\n"
    "trim_trailing_whitespace = true\n"
)

_BINARY_SUFFIXES = {
    ".7z",
    ".a",
    ".bin",
    ".dll",
    ".dylib",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".o",
    ".pdf",
    ".png",
    ".so",
    ".tar",
    ".tgz",
    ".ttf",
    ".vsix",
    ".woff",
    ".woff2",
    ".zip",
}
_PROHIBITED_ARTIFACT_SUFFIXES = {
    ".7z",
    ".a",
    ".apk",
    ".appimage",
    ".bin",
    ".bz2",
    ".ckpt",
    ".dmg",
    ".dll",
    ".dylib",
    ".engine",
    ".exe",
    ".gguf",
    ".gz",
    ".iso",
    ".jar",
    ".lib",
    ".mlmodel",
    ".msi",
    ".o",
    ".obj",
    ".onnx",
    ".ot",
    ".pdb",
    ".pkg",
    ".pt",
    ".pth",
    ".rar",
    ".safetensors",
    ".so",
    ".tar",
    ".tflite",
    ".tgz",
    ".vsix",
    ".wasm",
    ".whl",
    ".xz",
    ".zip",
}
_PROHIBITED_ARTIFACT_PARTS = {
    ".tools",
    ".venv",
    "artifacts",
    "models",
    "node_modules",
    "toolchains",
}
_COMPILED_ARTIFACT_MAGICS = (
    b"MZ",
    b"\x7fELF",
    b"\x00asm",
    b"\xca\xfe\xba\xbe",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)
_WALK_EXCLUDED_PARTS = {".git", ".superpowers", "reports"}


@dataclass
class CommandEvidence:
    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    cwd: str = "."
    duration_ms: int = 0


@dataclass
class CheckResult:
    check_id: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)
    commands: list[CommandEvidence] = field(default_factory=list)


def make_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _source_files(root: Path) -> list[Path]:
    root = root.resolve()
    if (root / ".git").exists():
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode == 0:
            paths: list[Path] = []
            for raw_name in completed.stdout.split(b"\0"):
                if not raw_name:
                    continue
                name = os.fsdecode(raw_name)
                path = root / name
                if path.is_file() or path.is_symlink():
                    paths.append(path)
            return sorted(paths, key=lambda item: _relative(root, item))

    paths = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _WALK_EXCLUDED_PARTS for part in relative.parts):
            continue
        paths.append(path)
    return sorted(paths, key=lambda item: _relative(root, item))


def _tracked_eol_metadata(root: Path) -> dict[str, tuple[str, str]]:
    if not (root / ".git").exists():
        return {}
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--eol", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    metadata: dict[str, tuple[str, str]] = {}
    for raw_record in completed.stdout.split(b"\0"):
        if not raw_record:
            continue
        record = raw_record.decode("utf-8", errors="replace")
        fields, separator, name = record.partition("\t")
        parts = fields.split()
        if not separator or len(parts) < 2:
            continue
        metadata[name] = (
            parts[0].removeprefix("i/"),
            parts[1].removeprefix("w/"),
        )
    return metadata


def _is_text(path: Path, data: bytes) -> bool:
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return False
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _run_command(
    argv: Sequence[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
) -> CommandEvidence:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=merged_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except FileNotFoundError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nTimed out after {timeout} seconds."
    duration_ms = int((time.monotonic() - started) * 1000)
    return CommandEvidence(
        argv=list(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(cwd.resolve()),
        duration_ms=duration_ms,
    )


def _short_command_error(command: CommandEvidence, root: Path) -> str:
    text = (command.stderr or command.stdout).strip()
    if not text:
        return f"command exited {command.exit_code}"
    text = text.replace(str(root.resolve()), "<ROOT>")
    text = text.replace(str(root.resolve()).replace("\\", "/"), "<ROOT>")
    if len(text) > 4000:
        text = text[-4000:]
    return text


def _find_powershell() -> str | None:
    configured = os.environ.get("ORACLE_POWERSHELL")
    if configured and Path(configured).is_file():
        return configured
    if os.name == "nt":
        found = shutil.which("powershell")
        if found:
            return found
        fixed = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if fixed.is_file():
            return str(fixed)
    return shutil.which("pwsh")


def _find_bash() -> str | None:
    configured = os.environ.get("ORACLE_BASH")
    if configured and Path(configured).is_file():
        return configured
    if os.name == "nt":
        fixed = Path("C:/Program Files/Git/bin/bash.exe")
        if fixed.is_file():
            return str(fixed)
        return None
    return shutil.which("bash") or ("/bin/bash" if Path("/bin/bash").is_file() else None)


def check_powershell(root: Path) -> CheckResult:
    root = root.resolve()
    files = [path for path in _source_files(root) if path.suffix.lower() == ".ps1"]
    non_ascii = []
    for path in files:
        data = path.read_bytes()
        if any(byte > 0x7F for byte in data):
            non_ascii.append(f"{_relative(root, path)}: contains non-ASCII bytes")

    executable = _find_powershell()
    if not executable:
        return CheckResult(
            "powershell",
            FAIL,
            "Required PowerShell parser is unavailable",
            non_ascii
            + [
                f"Could not AST-check {len(files)} PowerShell file(s); "
                "required static gates cannot be skipped."
            ],
        )

    parser_script = (
        "$ErrorActionPreference = 'Continue'; "
        "$files = ConvertFrom-Json $env:ORACLE_VERIFY_PS_FILES; "
        "$failed = 0; "
        "foreach ($file in $files) { "
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$file, [ref]$tokens, [ref]$errors) | Out-Null; "
        "foreach ($parseError in $errors) { "
        "Write-Error (('{0}:{1}: {2}' -f $file, "
        "$parseError.Extent.StartLineNumber, $parseError.Message)); "
        "$failed = 1 } }; "
        "exit $failed"
    )
    command = _run_command(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            parser_script,
        ],
        root,
        env={"ORACLE_VERIFY_PS_FILES": json.dumps([str(path) for path in files])},
    )
    details = list(non_ascii)
    if command.exit_code != 0:
        details.append(_short_command_error(command, root))
    status = FAIL if details else PASS
    summary = (
        f"{len(files)} PowerShell file(s) passed ASCII and AST checks"
        if status == PASS
        else "PowerShell ASCII or AST validation failed"
    )
    return CheckResult("powershell", status, summary, details, [command])


def _is_bash_entrypoint(path: Path) -> bool:
    if path.suffix.lower() == ".sh":
        return True
    if path.suffix:
        return False
    try:
        first_line = path.read_bytes().splitlines()[0]
    except (IndexError, OSError):
        return False
    return b"bash" in first_line and first_line.startswith(b"#!")


def _script_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".ps1":
        return "powershell"
    if suffix == ".sh":
        return "posix"
    try:
        first_line = path.read_bytes().splitlines()[0].lower()
    except (IndexError, OSError):
        return None
    if not first_line.startswith(b"#!"):
        return None
    if any(shell in first_line for shell in (b"bash", b"/sh", b"zsh", b"ksh")):
        return "posix"
    return "portable"


def check_bash(root: Path) -> CheckResult:
    root = root.resolve()
    files = [path for path in _source_files(root) if _is_bash_entrypoint(path)]
    executable = _find_bash()
    if not executable:
        return CheckResult(
            "bash",
            FAIL,
            "Required Bash parser is unavailable",
            [
                f"Could not syntax-check {len(files)} Bash file(s); "
                "required static gates cannot be skipped."
            ],
        )
    if not files:
        return CheckResult("bash", PASS, "No Bash source files found")
    command = _run_command(
        [executable, "-n", *[str(path) for path in files]],
        root,
    )
    if command.exit_code != 0:
        return CheckResult(
            "bash",
            FAIL,
            "Bash syntax validation failed",
            [_short_command_error(command, root)],
            [command],
        )
    return CheckResult(
        "bash",
        PASS,
        f"{len(files)} Bash file(s) passed syntax validation",
        commands=[command],
    )


def check_python(root: Path) -> CheckResult:
    root = root.resolve()
    files = [path for path in _source_files(root) if path.suffix.lower() == ".py"]
    compiler = (
        "import json, os, pathlib, sys\n"
        "failed = 0\n"
        "for name in json.loads(os.environ['ORACLE_VERIFY_PY_FILES']):\n"
        "    try:\n"
        "        source = pathlib.Path(name).read_text(encoding='utf-8')\n"
        "        compile(source, name, 'exec', dont_inherit=True)\n"
        "    except Exception as exc:\n"
        "        print(f'{name}: {exc}', file=sys.stderr)\n"
        "        failed = 1\n"
        "raise SystemExit(failed)\n"
    )
    command = _run_command(
        [sys.executable, "-c", compiler],
        root,
        env={
            "ORACLE_VERIFY_PY_FILES": json.dumps([str(path) for path in files]),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if command.exit_code != 0:
        return CheckResult(
            "python",
            FAIL,
            "Python compilation failed",
            [_short_command_error(command, root)],
            [command],
        )
    return CheckResult(
        "python",
        PASS,
        f"{len(files)} Python file(s) compiled without cache writes",
        commands=[command],
    )


def check_conductor_tests(root: Path) -> CheckResult:
    root = root.resolve()
    tests = root / "conductor" / "tests"
    if not tests.is_dir():
        return CheckResult(
            "conductor_tests",
            FAIL,
            "Required conductor test directory is absent",
        )
    command = _run_command(
        [sys.executable, "-m", "pytest", "conductor/tests", "-q"],
        root,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
        },
        timeout=600,
    )
    if command.exit_code != 0:
        return CheckResult(
            "conductor_tests",
            FAIL,
            "Conductor pytest suite failed",
            [_short_command_error(command, root)],
            [command],
        )
    final_line = next(
        (line for line in reversed(command.stdout.splitlines()) if line.strip()),
        "pytest passed",
    )
    return CheckResult(
        "conductor_tests",
        PASS,
        f"Conductor suite passed: {final_line.strip()}",
        commands=[command],
    )


def check_line_policy(root: Path) -> CheckResult:
    root = root.resolve()
    details = []
    tracked_eol = _tracked_eol_metadata(root)
    attributes = root / ".gitattributes"
    editorconfig = root / ".editorconfig"
    if not attributes.is_file():
        details.append(".gitattributes: missing")
    elif attributes.read_bytes().replace(b"\r\n", b"\n") != (
        REQUIRED_GITATTRIBUTES.encode("ascii")
    ):
        details.append(".gitattributes: content differs from deterministic LF policy")
    if not editorconfig.is_file():
        details.append(".editorconfig: missing")
    elif editorconfig.read_bytes().replace(b"\r\n", b"\n") != (
        REQUIRED_EDITORCONFIG.encode("ascii")
    ):
        details.append(".editorconfig: content differs from deterministic editor policy")

    text_count = 0
    for path in _source_files(root):
        data = path.read_bytes()
        relative = _relative(root, path)
        if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
            details.append(f"{relative}: BOM is not allowed")
            if relative not in tracked_eol and b"\r\n" in data:
                details.append(f"{relative}: CRLF is not allowed")
            if b"\r" in data.replace(b"\r\n", b""):
                details.append(f"{relative}: bare CR is not allowed")
            continue
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            details.append(f"{relative}: source is not valid UTF-8")
            continue
        if b"\0" in data:
            details.append(f"{relative}: NUL bytes indicate a prohibited encoding")
            continue
        text_count += 1
        if relative not in tracked_eol and b"\r\n" in data:
            details.append(f"{relative}: CRLF is not allowed")
        if b"\r" in data.replace(b"\r\n", b""):
            details.append(f"{relative}: bare CR is not allowed")

    for relative, (index_eol, _worktree_eol) in sorted(tracked_eol.items()):
        if index_eol in {"crlf", "mixed"}:
            details.append(
                f"{relative}: tracked blob uses CRLF or mixed EOL (i/{index_eol})"
            )

    status = FAIL if details else PASS
    summary = (
        f"{text_count} text file(s) follow LF/no-BOM policy"
        if status == PASS
        else "Line-ending or repository text policy failed"
    )
    return CheckResult("line_policy", status, summary, details)


def check_platform_twins(
    root: Path, policy: Mapping[str, Any] | None = None
) -> CheckResult:
    root = root.resolve()
    policy = policy or {}
    scripts = {
        _relative(root, path): kind
        for path in _source_files(root)
        if (kind := _script_kind(path)) is not None
    }
    platform_files = {
        path for path, kind in scripts.items() if kind in {"powershell", "posix"}
    }
    details: list[str] = []
    claimed: set[str] = {
        path for path, kind in scripts.items() if kind == "portable"
    }

    aliases = policy.get("platform_twins", [])
    if not isinstance(aliases, list):
        details.append("policy.platform_twins must be a list")
        aliases = []
    for index, alias in enumerate(aliases):
        if not isinstance(alias, dict):
            details.append(f"platform_twins[{index}] must be an object")
            continue
        powershell = alias.get("powershell", "")
        posix = alias.get("posix", "")
        reason = alias.get("reason", "")
        if not powershell or not posix or not reason:
            details.append(
                f"platform_twins[{index}] needs powershell, posix, and reason"
            )
            continue
        if not (root / powershell).is_file():
            details.append(f"platform twin missing: {powershell}")
        if not (root / posix).is_file():
            details.append(f"platform twin missing: {posix}")
        claimed.update({powershell, posix})

    scope_entries = policy.get("platform_scoped", [])
    if not isinstance(scope_entries, list):
        details.append("policy.platform_scoped must be a list")
        scope_entries = []
    scope: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(scope_entries):
        if not isinstance(entry, dict):
            details.append(f"platform_scoped[{index}] must be an object")
            continue
        path = entry.get("path", "")
        target = entry.get("platform", "")
        reason = entry.get("reason", "")
        if path in scope:
            details.append(f"duplicate platform scope entry: {path}")
        if target not in {"windows", "macos", "linux", "posix"}:
            details.append(f"{path or index}: invalid platform scope {target!r}")
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            details.append(f"{path or index}: platform scope needs a concrete reason")
        if not path or not (root / path).is_file():
            details.append(f"platform scope path is missing: {path or index}")
        if path:
            scope[path] = entry

    for path in sorted(platform_files):
        if path in claimed:
            continue
        source = Path(path)
        if scripts[path] == "powershell":
            counterpart = source.with_suffix(".sh").as_posix()
        elif source.suffix.lower() == ".sh":
            counterpart = source.with_suffix(".ps1").as_posix()
        else:
            counterpart = f"{path}.ps1"
        if counterpart in platform_files:
            claimed.update({path, counterpart})
            continue
        if path in scope:
            claimed.add(path)
            continue
        details.append(
            f"{path}: missing {counterpart} twin and no platform scope entry"
        )

    for path in sorted(scope):
        if path not in platform_files:
            continue
        source = Path(path)
        if scripts[path] == "powershell":
            counterpart = source.with_suffix(".sh").as_posix()
        elif source.suffix.lower() == ".sh":
            counterpart = source.with_suffix(".ps1").as_posix()
        else:
            counterpart = f"{path}.ps1"
        if counterpart in platform_files:
            details.append(
                f"{path}: stale platform scope entry; twin {counterpart} exists"
            )

    status = FAIL if details else PASS
    summary = (
        f"{len(scripts)} tracked script(s) are portable, paired, or explicitly scoped"
        if status == PASS
        else "Platform twin inventory failed"
    )
    return CheckResult("platform_twins", status, summary, details)


def _data_lines(path: Path) -> list[tuple[int, str]]:
    lines = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append((number, stripped))
    return lines


def check_model_integrity(root: Path) -> CheckResult:
    root = root.resolve()
    manifest_path = root / "serving" / "models.manifest"
    profiles_path = root / "serving" / "profiles.conf"
    details = []
    if not manifest_path.is_file():
        details.append("serving/models.manifest: missing")
    if not profiles_path.is_file():
        details.append("serving/profiles.conf: missing")
    if details:
        return CheckResult(
            "model_integrity", FAIL, "Model source-of-truth files are missing", details
        )

    models: dict[str, dict[str, Any]] = {}
    for line_number, line in _data_lines(manifest_path):
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 6:
            details.append(
                f"serving/models.manifest:{line_number}: expected 6 fields"
            )
            continue
        name, repository, include, slot, context, _flags = fields
        if not name or name in models:
            details.append(
                f"serving/models.manifest:{line_number}: empty or duplicate model {name!r}"
            )
            continue
        if not repository or not include:
            details.append(
                f"serving/models.manifest:{line_number}: repository/include is empty"
            )
        if slot not in {"big", "fast", "embed"}:
            details.append(
                f"serving/models.manifest:{line_number}: invalid slot {slot!r}"
            )
        try:
            parsed_context = int(context)
            if parsed_context <= 0:
                raise ValueError
        except ValueError:
            parsed_context = 0
            details.append(
                f"serving/models.manifest:{line_number}: invalid context {context!r}"
            )
        models[name] = {"slot": slot, "context": parsed_context}

    profile_names = set()
    for line_number, line in _data_lines(profiles_path):
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 7:
            details.append(f"serving/profiles.conf:{line_number}: expected 7 fields")
            continue
        name, memory, model_csv, opus, sonnet, haiku, _size = fields
        if not name or name in profile_names:
            details.append(
                f"serving/profiles.conf:{line_number}: empty or duplicate profile {name!r}"
            )
        profile_names.add(name)
        try:
            if int(memory) <= 0:
                raise ValueError
        except ValueError:
            details.append(
                f"serving/profiles.conf:{line_number}: invalid memory {memory!r}"
            )
        selected = [item.strip() for item in model_csv.split(",") if item.strip()]
        selected_set = set(selected)
        for model in selected:
            if model not in models:
                details.append(
                    f"serving/profiles.conf:{line_number}: unknown model {model}"
                )
        for tier, model in (("opus", opus), ("sonnet", sonnet), ("haiku", haiku)):
            if model not in models:
                details.append(
                    f"serving/profiles.conf:{line_number}: {tier} tier uses unknown model {model}"
                )
            elif model not in selected_set:
                details.append(
                    f"serving/profiles.conf:{line_number}: {tier} tier model {model} is not selected"
                )
            elif models[model]["slot"] == "embed":
                details.append(
                    f"serving/profiles.conf:{line_number}: {tier} tier cannot use embed model {model}"
                )
        slots = {models[item]["slot"] for item in selected if item in models}
        if "embed" not in slots:
            details.append(
                f"serving/profiles.conf:{line_number}: profile has no embedding model"
            )
        if not slots.intersection({"big", "fast"}):
            details.append(
                f"serving/profiles.conf:{line_number}: profile has no chat model"
            )

    active_path = root / "serving" / "models.profile"
    active: set[str] | None = None
    if active_path.is_file():
        active = {line for _number, line in _data_lines(active_path)}
        for model in sorted(active - models.keys()):
            details.append(f"serving/models.profile: unknown model {model}")

    tiers_path = root / "serving" / "tiers.env"
    if tiers_path.is_file():
        tiers = {}
        for line_number, line in _data_lines(tiers_path):
            if "=" not in line:
                details.append(f"serving/tiers.env:{line_number}: expected KEY=value")
                continue
            key, value = [item.strip() for item in line.split("=", 1)]
            tiers[key] = value
        for key in ("OPUS_MODEL", "SONNET_MODEL", "HAIKU_MODEL"):
            value = tiers.get(key)
            if not value:
                details.append(f"serving/tiers.env: missing {key}")
            elif value not in models:
                details.append(f"serving/tiers.env: {key} uses unknown model {value}")
            elif models[value]["slot"] == "embed":
                details.append(f"serving/tiers.env: {key} uses embedding model {value}")
            elif active is not None and value not in active:
                details.append(
                    f"serving/tiers.env: {key} model {value} is not in models.profile"
                )

    status = FAIL if details else PASS
    summary = (
        f"{len(models)} model(s) and {len(profile_names)} profile(s) are referentially sound"
        if status == PASS
        else "Profile/model/tier referential integrity failed"
    )
    return CheckResult("model_integrity", status, summary, details)


def _strip_jsonc(text: str) -> str:
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            output.extend("\n" for char in text[index : end + 2] if char == "\n")
            index = end + 2
            continue
        output.append(char)
        index += 1
    if in_string:
        raise ValueError("unterminated JSON string")
    without_comments = "".join(output)
    return re.sub(r",(\s*[}\]])", r"\1", without_comments)


def _yaml_content_without_comment(content: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(content):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or content[index - 1].isspace()):
            return content[:index].rstrip()
    if quote:
        raise ValueError("unterminated quoted scalar")
    return content.rstrip()


def _yaml_mapping_colon(content: str) -> int:
    quote = ""
    escaped = False
    depth = 0
    for index, char in enumerate(content):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced flow collection")
        elif char == ":" and depth == 0:
            return index
    if quote or depth:
        raise ValueError("unterminated YAML quote or flow collection")
    return -1


def _validate_yaml_flow(content: str) -> None:
    quote = ""
    escaped = False
    stack = []
    pairs = {"]": "[", "}": "{", ")": "("}
    for char in content:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{(":
            stack.append(char)
        elif char in "]})":
            if not stack or stack.pop() != pairs[char]:
                raise ValueError("unbalanced flow collection")
    if quote or stack:
        raise ValueError("unterminated YAML quote or flow collection")


def _split_yaml_flow_items(content: str) -> list[str]:
    if not content.strip():
        return []
    items = []
    start = 0
    quote = ""
    escaped = False
    stack = []
    pairs = {"]": "[", "}": "{", ")": "("}
    for index, char in enumerate(content):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{(":
            stack.append(char)
        elif char in "]})":
            if not stack or stack.pop() != pairs[char]:
                raise ValueError("unbalanced flow collection")
        elif char == "," and not stack:
            item = content[start:index].strip()
            if not item:
                raise ValueError("empty item in flow collection")
            items.append(item)
            start = index + 1
    if quote or stack:
        raise ValueError("unterminated flow collection item")
    final = content[start:].strip()
    if not final:
        raise ValueError("empty item in flow collection")
    items.append(final)
    return items


def _validate_yaml_flow_value(value: str) -> None:
    text = value.strip()
    if not text:
        raise ValueError("empty flow value")
    if text[0] not in "[{":
        _validate_yaml_flow(text)
        return
    closer = "]" if text[0] == "[" else "}"
    quote = ""
    escaped = False
    stack = []
    end = -1
    pairs = {"]": "[", "}": "{", ")": "("}
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[{(":
            stack.append(char)
        elif char in "]})":
            if not stack or stack.pop() != pairs[char]:
                raise ValueError("unbalanced flow collection")
            if not stack:
                end = index
                break
    if quote or stack or end < 0 or text[end] != closer:
        raise ValueError("unterminated flow collection")
    if text[end + 1 :].strip():
        raise ValueError("unexpected content after flow collection")

    items = _split_yaml_flow_items(text[1:end])
    if text[0] == "[":
        for item in items:
            _validate_yaml_flow_value(item)
        return
    for item in items:
        colon = _yaml_mapping_colon(item)
        if colon < 1:
            raise ValueError("flow mapping item needs a key and value")
        key = item[:colon].strip()
        item_value = item[colon + 1 :].strip()
        if not key or not item_value:
            raise ValueError("flow mapping item needs a key and value")
        _validate_yaml_flow_value(item_value)


def _parse_yaml_subset(text: str) -> None:
    levels = [0]
    can_indent = False
    seen_content = False
    block_parent: int | None = None
    for line_number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError(f"line {line_number}: tabs are not valid indentation")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = _yaml_content_without_comment(raw[indent:])
        if not content:
            continue
        if block_parent is not None:
            if indent > block_parent:
                continue
            block_parent = None
        if content in {"---", "..."}:
            continue
        if not seen_content:
            if indent != 0:
                raise ValueError(f"line {line_number}: document must start at column 1")
            seen_content = True
        if indent > levels[-1]:
            if not can_indent:
                raise ValueError(f"line {line_number}: unexpected indentation")
            levels.append(indent)
        elif indent < levels[-1]:
            while len(levels) > 1 and indent < levels[-1]:
                levels.pop()
            if indent != levels[-1]:
                raise ValueError(f"line {line_number}: inconsistent indentation")

        is_list = content == "-" or content.startswith("- ")
        body = content[1:].lstrip() if is_list else content
        _validate_yaml_flow(body)
        if not body:
            can_indent = True
            continue
        colon = _yaml_mapping_colon(body)
        if is_list and colon < 0:
            _validate_yaml_flow_value(body)
            can_indent = False
            continue
        if colon < 1:
            raise ValueError(f"line {line_number}: expected a mapping key")
        key = body[:colon].strip()
        value = body[colon + 1 :].strip()
        if not key:
            raise ValueError(f"line {line_number}: empty mapping key")
        if value and value not in {"|", ">", "|-", ">-", "|+", ">+"}:
            _validate_yaml_flow_value(value)
        can_indent = not value or is_list
        if value in {"|", ">", "|-", ">-", "|+", ">+"}:
            block_parent = indent
            can_indent = True


def check_config_formats(root: Path) -> CheckResult:
    root = root.resolve()
    supported = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".plist"}
    files = [path for path in _source_files(root) if path.suffix.lower() in supported]
    details = []
    counts = {suffix: 0 for suffix in supported}
    for path in files:
        suffix = path.suffix.lower()
        counts[suffix] += 1
        relative = _relative(root, path)
        try:
            data = path.read_bytes()
            if suffix == ".json":
                json.loads(data.decode("utf-8"))
            elif suffix == ".jsonc":
                json.loads(_strip_jsonc(data.decode("utf-8")))
            elif suffix == ".toml":
                tomllib.loads(data.decode("utf-8"))
            elif suffix in {".yaml", ".yml"}:
                _parse_yaml_subset(data.decode("utf-8"))
            else:
                plistlib.loads(data)
        except Exception as exc:
            details.append(f"{relative}: {type(exc).__name__}: {exc}")
    status = FAIL if details else PASS
    count_text = ", ".join(
        f"{suffix[1:]}={count}"
        for suffix, count in sorted(counts.items())
        if count
    )
    summary = (
        f"{len(files)} configuration file(s) parsed ({count_text or 'none'})"
        if status == PASS
        else "Configuration parsing failed"
    )
    return CheckResult("config_formats", status, summary, details)


def check_path_safety(root: Path, scratch_dir: Path) -> CheckResult:
    root = root.resolve()
    fixture = scratch_dir / "path with spaces"
    try:
        fixture.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return CheckResult(
            "path_safety",
            FAIL,
            "Path fixture would overwrite existing evidence",
            [str(fixture)],
        )
    configured_path = root / "generated path with spaces" / "model.gguf"
    config_path = fixture / "generated config.json"
    output_path = fixture / "command output.json"
    helper_path = fixture / "command helper.py"
    config_path.write_text(
        json.dumps({"model_path": str(configured_path)}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    helper_path.write_text(
        "import json, pathlib, sys\n"
        "source = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "payload = {'model_path': source['model_path'], 'argv': sys.argv[1:]}\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    command = _run_command(
        [sys.executable, str(helper_path), str(config_path), str(output_path)],
        root,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    details = []
    if command.exit_code != 0:
        details.append(_short_command_error(command, root))
    else:
        try:
            generated = json.loads(output_path.read_text(encoding="utf-8"))
            if generated["model_path"] != str(configured_path):
                details.append("generated config did not preserve its spaced path")
            if generated["argv"] != [str(config_path), str(output_path)]:
                details.append("argv construction did not preserve spaced arguments")
        except Exception as exc:
            details.append(f"could not parse generated path fixture: {exc}")
    status = FAIL if details else PASS
    summary = (
        "Generated config and argv preserve paths with spaces"
        if status == PASS
        else "Path-with-spaces fixture failed"
    )
    return CheckResult("path_safety", status, summary, details, [command])


def check_package_allowlist(
    root: Path, policy: Mapping[str, Any] | None = None
) -> CheckResult:
    root = root.resolve()
    policy = policy or {}
    allowlist = policy.get("package_allowlist")
    if not isinstance(allowlist, dict):
        return CheckResult(
            "package_allowlist",
            FAIL,
            "Package allowlist is missing",
            ["verification/policy.json needs package_allowlist.roots/files."],
        )
    roots = allowlist.get("roots", [])
    files = allowlist.get("files", [])
    source_assets = allowlist.get("source_assets", [])
    if (
        not isinstance(roots, list)
        or not isinstance(files, list)
        or not isinstance(source_assets, list)
    ):
        return CheckResult(
            "package_allowlist",
            FAIL,
            "Package allowlist is malformed",
            [
                "package_allowlist.roots, .files, and .source_assets "
                "must be lists."
            ],
        )
    allowed_roots = set(roots)
    allowed_files = set(files)
    details = []
    source_files = _source_files(root)
    for path in source_files:
        relative = _relative(root, path)
        first = relative.split("/", 1)[0]
        if relative not in allowed_files and first not in allowed_roots:
            details.append(f"{relative}: outside package allowlist")

        data = path.read_bytes()
        path_parts = {part.lower() for part in relative.split("/")[:-1]}
        suffix = path.suffix.lower()
        if path_parts.intersection(_PROHIBITED_ARTIFACT_PARTS):
            details.append(f"{relative}: prohibited generated artifact directory")
            continue
        if suffix in _PROHIBITED_ARTIFACT_SUFFIXES:
            details.append(f"{relative}: prohibited binary, model, or archive artifact")
            continue
        if data.startswith(_COMPILED_ARTIFACT_MAGICS):
            details.append(f"{relative}: prohibited compiled binary content")
            continue

        declared_source_asset = any(
            isinstance(pattern, str) and fnmatch.fnmatch(relative, pattern)
            for pattern in source_assets
        )
        if not _is_text(path, data) and not declared_source_asset:
            details.append(f"{relative}: undeclared binary source asset")
    status = FAIL if details else PASS
    summary = (
        f"{len(source_files)} source file(s) are package-allowlisted"
        if status == PASS
        else "Package allowlist rejected source or prohibited artifact files"
    )
    return CheckResult("package_allowlist", status, summary, details)


def _is_obvious_secret_placeholder(value: str) -> bool:
    lowered = value.lower()
    markers = (
        "changeme",
        "dummy",
        "example",
        "fake",
        "placeholder",
        "redacted",
        "replace",
        "sample",
        "your_",
    )
    if any(marker in lowered for marker in markers):
        return True

    body = re.sub(
        r"^(?:pypi-AgEIcHlwaS5vcmcC|sk-ant-(?:api\d{2}-)?|"
        r"github_pat_|ghp_|hf_|sk-|npm_|pypi-)",
        "",
        lowered,
        flags=re.IGNORECASE,
    )
    alphanumeric = "".join(char for char in body if char.isalnum())
    if alphanumeric and len(set(alphanumeric)) <= 3:
        return True
    return any(
        sequence in alphanumeric
        for sequence in (
            "0123456789",
            "1234567890",
            "abcdefghijklmnopqrstuvwxyz",
        )
    )


def check_secrets(root: Path) -> CheckResult:
    root = root.resolve()
    patterns = [
        ("GitHub token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{30,}\b")),
        ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
        (
            "Anthropic API key",
            re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{40,}\b"),
        ),
        ("npm access token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
        (
            "PyPI API token",
            re.compile(r"\bpypi-AgEIcHlwaS5vcmcC[A-Za-z0-9_-]{30,}\b"),
        ),
        ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
        (
            "private key",
            re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
        ),
        ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ]
    details = []
    scanned = 0
    for path in _source_files(root):
        data = path.read_bytes()
        if not _is_text(path, data):
            continue
        scanned += 1
        text = data.decode("utf-8")
        for label, pattern in patterns:
            if any(
                not _is_obvious_secret_placeholder(match.group(0))
                for match in pattern.finditer(text)
            ):
                details.append(f"{_relative(root, path)}: possible {label}")
    status = FAIL if details else PASS
    summary = (
        f"{scanned} text file(s) contain no recognized secret shapes"
        if status == PASS
        else "Possible committed secrets detected"
    )
    return CheckResult("secrets", status, summary, details)


def check_host_generated(root: Path) -> CheckResult:
    root = root.resolve()
    generated_patterns = (
        "**/.DS_Store",
        "**/Thumbs.db",
        "**/desktop.ini",
        "**/__pycache__/*",
        "**/.pytest_cache/*",
        "**/*.pyc",
        "**/*.pyo",
        "**/*.tmp",
        "**/*.log",
        "serving/llama-swap.rendered.yaml",
        "serving/llama-swap.rendered.win.yaml",
        "serving/models.profile",
        "serving/tiers.env",
        ".install-state",
        ".agent/*",
        "node_modules/*",
    )
    details = []
    files = _source_files(root)
    for path in files:
        relative = _relative(root, path)
        if any(
            fnmatch.fnmatch(relative, pattern)
            or fnmatch.fnmatch("/" + relative, pattern)
            for pattern in generated_patterns
        ):
            details.append(f"{relative}: host-generated file")

    placeholder_users = {"name", "user", "username", "me", "<user>"}
    windows_user = re.compile(
        r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s\"'<>]+)[\\/]"
    )
    mac_user = re.compile(
        r"(?<![A-Za-z0-9_.-])/" + r"Users" + r"/([^/\s\"'<>]+)/"
    )
    linux_user = re.compile(
        r"(?<![A-Za-z0-9_.-])/" + r"home" + r"/([^/\s\"'<>]+)/"
    )
    for path in files:
        data = path.read_bytes()
        if not _is_text(path, data):
            continue
        text = data.decode("utf-8")
        for pattern in (windows_user, mac_user, linux_user):
            match = pattern.search(text)
            if match and match.group(1).lower() not in placeholder_users:
                details.append(
                    f"{_relative(root, path)}: host-specific user path"
                )
                break
    status = FAIL if details else PASS
    summary = (
        "No host-generated files or host-specific user paths are source-controlled"
        if status == PASS
        else "Host-generated source hygiene failed"
    )
    return CheckResult("host_generated", status, summary, details)


def check_oversized(
    root: Path, max_bytes: int = MAX_SOURCE_BYTES
) -> CheckResult:
    root = root.resolve()
    details = []
    largest = 0
    files = _source_files(root)
    for path in files:
        size = path.stat().st_size
        largest = max(largest, size)
        if size >= max_bytes:
            details.append(
                f"{_relative(root, path)}: {size} bytes meets or exceeds {max_bytes}"
            )
    status = FAIL if details else PASS
    summary = (
        f"{len(files)} source file(s) are smaller than {max_bytes} bytes"
        if status == PASS
        else "Oversized source files detected"
    )
    return CheckResult(
        "oversized_files",
        status,
        summary,
        details + ([] if files else ["No source files found."]),
    )


def _clean_command_path(candidate: str) -> str:
    cleaned = candidate.strip("`'\"()[]{}<>,;:").rstrip(".")
    return cleaned.replace("\\", "/").removeprefix("./")


def _documented_path_variants(candidate: str) -> list[str]:
    cleaned = _clean_command_path(candidate)
    if not cleaned or any(character in cleaned for character in "<>{}$*?"):
        return []
    if "|" not in cleaned:
        return [cleaned]

    base, *alternatives = cleaned.split("|")
    variants = [base]
    for alternative in alternatives:
        if not alternative.startswith(".") or "/" in alternative:
            return []
        variants.append(str(Path(base).with_suffix(alternative)).replace("\\", "/"))
    return variants


def _fenced_local_commands(text: str) -> list[tuple[int, str]]:
    shell_languages = {
        "bash",
        "ksh",
        "powershell",
        "ps1",
        "pwsh",
        "sh",
        "shell",
        "zsh",
    }
    fence: tuple[str, int, bool] | None = None
    opener = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)\s*$")
    command = re.compile(
        r"^\s*(?:PS>\s+|\$\s+)?(?:&\s*)?[\"']?"
        r"((?:\./|\.\\)?(?:bin|bootstrap|conductor|connectors|engines|harness|"
        r"serving|verification)[\\/][A-Za-z0-9_./\\-]+)"
        r"[\"']?(?:\s|$)",
        re.IGNORECASE,
    )
    candidates = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            marker, minimum_length, is_shell = fence
            if re.fullmatch(
                rf"\s*{re.escape(marker)}{{{minimum_length},}}\s*", line
            ):
                fence = None
                continue
            if is_shell:
                match = command.match(line)
                if match:
                    candidates.extend(
                        (line_number, candidate)
                        for candidate in _documented_path_variants(match.group(1))
                    )
            continue

        match = opener.match(line)
        if match:
            delimiter = match.group(1)
            language = match.group(2).lower()
            fence = (
                delimiter[0],
                len(delimiter),
                language in shell_languages,
            )
    return candidates


def check_docs_commands(root: Path) -> CheckResult:
    root = root.resolve()
    details = []
    command_patterns = [
        re.compile(
            r"\b(?:bash|sh)\s+((?:\./)?(?:bin|bootstrap|conductor|connectors|"
            r"engines|harness|serving|verification)[\\/][A-Za-z0-9_./\\-]+)"
        ),
        re.compile(
            r"\bpowershell(?:\.exe)?\b[^\n`]*?\s-File\s+([^\s`]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bpython(?:3)?\s+((?:\./)?(?:bin|bootstrap|conductor|connectors|"
            r"engines|harness|serving|verification)[\\/][A-Za-z0-9_./\\-]+)"
        ),
    ]
    inline_path = re.compile(
        r"`((?:\./)?(?:bin|bootstrap|conductor|connectors|engines|harness|"
        r"serving|verification)[\\/][^`\s]+)([^`]*)`"
    )
    command_cue = re.compile(
        r"\b(?:call|execute|invoke|launch|run|use)\s*[:(]?\s*$",
        re.IGNORECASE,
    )
    makefile = root / "Makefile"
    make_targets = set()
    if makefile.is_file():
        for line in makefile.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_-]+):(?:\s|$)", line)
            if match:
                make_targets.add(match.group(1))

    for path in _source_files(root):
        if path.suffix.lower() != ".md":
            continue
        relative_doc = _relative(root, path)
        project_doc = (
            relative_doc in {"README.md", "AGENTS.md", "LOOP.md"}
            or relative_doc.startswith("docs/")
        )
        text = path.read_text(encoding="utf-8")
        fenced_candidates: dict[int, set[str]] = {}
        for line_number, candidate in _fenced_local_commands(text):
            fenced_candidates.setdefault(line_number, set()).add(candidate)
        for line_number, line in enumerate(text.splitlines(), 1):
            line_candidates = set(fenced_candidates.get(line_number, set()))
            for pattern in command_patterns:
                for match in pattern.finditer(line):
                    line_candidates.update(
                        _documented_path_variants(match.group(1))
                    )
            for match in inline_path.finditer(line):
                has_arguments = bool(match.group(2).strip())
                has_command_cue = bool(command_cue.search(line[: match.start()]))
                if has_arguments or has_command_cue:
                    line_candidates.update(
                        _documented_path_variants(match.group(1))
                    )
            for candidate in sorted(line_candidates):
                if not (root / candidate).is_file():
                    details.append(
                        f"{_relative(root, path)}:{line_number}: command entry point "
                        f"does not exist: {candidate}"
                    )
            make_commands = (
                re.finditer(
                    r"`make\s+([A-Za-z0-9_-]+)`|"
                    r"^\s*(?:\$\s*)?make\s+([A-Za-z0-9_-]+)",
                    line,
                )
                if project_doc
                else ()
            )
            for match in make_commands:
                target = match.group(1) or match.group(2)
                if target not in make_targets:
                    details.append(
                        f"{_relative(root, path)}:{line_number}: Make target does not exist: {target}"
                    )
            if re.search(r"(?:^|[\s`])(?:\./)?install(?:[\s`]|$)", line):
                if not (root / "install").is_file():
                    details.append(
                        f"{_relative(root, path)}:{line_number}: install entry point is missing"
                    )
            if re.search(r"(?:^|[\s`])oracle(?:\s|`)", line):
                if not (root / "bin" / "oracle").is_file():
                    details.append(
                        f"{_relative(root, path)}:{line_number}: oracle entry point is missing"
                    )
    status = FAIL if details else PASS
    summary = (
        "Documented repository commands resolve to real entry points"
        if status == PASS
        else "Documentation references missing command entry points"
    )
    return CheckResult("docs_commands", status, summary, details)


def _sanitize_text(text: str, roots: Sequence[str]) -> str:
    sanitized = text
    replacements = []
    for root in roots:
        if not root or root == ".":
            continue
        replacements.extend(
            {
                root,
                root.replace("\\", "/"),
                root.replace("/", "\\"),
            }
        )
    home = str(Path.home())
    replacements.extend({home, home.replace("\\", "/"), home.replace("/", "\\")})
    for value in sorted(replacements, key=len, reverse=True):
        if value:
            sanitized = sanitized.replace(value, "<ROOT>")
    return sanitized


def _command_payload(command: CommandEvidence) -> dict[str, Any]:
    roots = [command.cwd]
    return {
        "argv": [_sanitize_text(str(arg), roots) for arg in command.argv],
        "cwd": ".",
        "duration_ms": command.duration_ms,
        "exit_code": command.exit_code,
        "stderr": _sanitize_text(command.stderr, roots),
        "stdout": _sanitize_text(command.stdout, roots),
    }


def _result_payload(result: CheckResult) -> dict[str, Any]:
    return {
        "id": result.check_id,
        "status": result.status,
        "summary": result.summary,
        "details": result.details,
        "commands": [_command_payload(command) for command in result.commands],
    }


def _overall_status(results: Sequence[CheckResult]) -> str:
    if any(result.status == FAIL for result in results):
        return FAIL
    if any(result.status in {SKIP, PROVISIONAL} for result in results):
        return PROVISIONAL
    return PASS


def _verification_exit_code(results: Sequence[CheckResult]) -> int:
    overall = _overall_status(results)
    if overall == PASS:
        return 0
    if overall == PROVISIONAL:
        return 2
    return 1


def _write_report_files(
    report_dir: Path,
    run_id: str,
    results: Sequence[CheckResult],
    metadata: Mapping[str, Any] | None,
) -> None:
    invalid = [result.status for result in results if result.status not in VALID_STATES]
    if invalid:
        raise ValueError(f"invalid verification states: {invalid}")
    counts = {
        state: sum(result.status == state for result in results)
        for state in VALID_STATES
    }
    payload = {
        "schema_version": VERIFIER_VERSION,
        "run_id": run_id,
        "overall_status": _overall_status(results),
        "counts": counts,
        "metadata": dict(metadata or {}),
        "checks": [_result_payload(result) for result in results],
    }
    report_path = report_dir / "report.json"
    summary_path = report_dir / "summary.txt"
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary_lines = [
        f"SentiVue Oracle verification {run_id}",
        f"Overall: {payload['overall_status']}",
        (
            "Counts: "
            + " ".join(f"{state}={counts[state]}" for state in VALID_STATES)
        ),
        "",
    ]
    for result in results:
        summary_lines.append(
            f"[{result.status}] {result.check_id}: {result.summary}"
        )
        summary_lines.extend(f"  - {detail}" for detail in result.details)
        for command in result.commands:
            summary_lines.append(
                f"  command exit={command.exit_code} duration_ms={command.duration_ms}: "
                + " ".join(_command_payload(command)["argv"])
            )
    summary_path.write_text(
        "\n".join(summary_lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksum_path = report_dir / "SHA256SUMS"
    evidence_files = sorted(
        (
            path
            for path in report_dir.rglob("*")
            if path.is_file() and path != checksum_path
        ),
        key=lambda path: path.relative_to(report_dir).as_posix(),
    )
    checksums = []
    for path in evidence_files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(report_dir).as_posix()
        checksums.append(f"{digest}  {relative}")
    checksum_path.write_text(
        "\n".join(checksums) + "\n",
        encoding="ascii",
        newline="\n",
    )


def write_reports(
    report_base: Path,
    run_id: str,
    results: Sequence[CheckResult],
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id):
        raise ValueError("run_id must be a portable file name")
    report_base.mkdir(parents=True, exist_ok=True)
    report_dir = report_base / run_id
    report_dir.mkdir(exist_ok=False)
    _write_report_files(report_dir, run_id, results, metadata)
    return report_dir


def _load_policy(root: Path) -> dict[str, Any]:
    path = root / "verification" / "policy.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("verification/policy.json must contain a JSON object")
    return data


def _source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _source_files(root):
        relative = _relative(root, path).encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _git_revision(root: Path) -> str:
    command = _run_command(["git", "rev-parse", "HEAD"], root)
    if command.exit_code != 0:
        return "unavailable"
    return command.stdout.strip()


def _execute_static_checks(
    root: Path, report_dir: Path, policy: Mapping[str, Any]
) -> list[CheckResult]:
    checks = [
        ("powershell", lambda: check_powershell(root)),
        ("bash", lambda: check_bash(root)),
        ("python", lambda: check_python(root)),
        ("conductor_tests", lambda: check_conductor_tests(root)),
        ("line_policy", lambda: check_line_policy(root)),
        ("platform_twins", lambda: check_platform_twins(root, policy)),
        ("model_integrity", lambda: check_model_integrity(root)),
        ("config_formats", lambda: check_config_formats(root)),
        ("path_safety", lambda: check_path_safety(root, report_dir / "artifacts")),
        ("package_allowlist", lambda: check_package_allowlist(root, policy)),
        ("secrets", lambda: check_secrets(root)),
        ("host_generated", lambda: check_host_generated(root)),
        ("oversized_files", lambda: check_oversized(root)),
        ("docs_commands", lambda: check_docs_commands(root)),
    ]
    results = []
    for check_id, check in checks:
        try:
            results.append(check())
        except Exception as exc:
            results.append(
                CheckResult(
                    check_id,
                    FAIL,
                    "Verifier check raised an unexpected exception",
                    [f"{type(exc).__name__}: {exc}"],
                )
            )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only SentiVue Oracle verification."
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Run static gates only (Task 1 default).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root.",
    )
    parser.add_argument("--run-id", help="Explicit unique run ID.")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    report_base = root / "reports" / "verification"
    run_id = args.run_id or make_run_id()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id):
        parser.error("--run-id must be a portable file name")
    report_base.mkdir(parents=True, exist_ok=True)
    report_dir = report_base / run_id
    try:
        report_dir.mkdir(exist_ok=False)
    except FileExistsError:
        print(f"verification: refusing to overwrite {report_dir}", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    try:
        policy = _load_policy(root)
    except Exception as exc:
        policy = {}
        policy_error = CheckResult(
            "policy",
            FAIL,
            "Verification policy could not be loaded",
            [f"{type(exc).__name__}: {exc}"],
        )
    else:
        policy_error = None

    source_digest = _source_digest(root)
    results = _execute_static_checks(root, report_dir, policy)
    if policy_error:
        results.insert(0, policy_error)
    finished_at = datetime.now(timezone.utc)
    metadata = {
        "mode": "static",
        "static_only": bool(args.static_only),
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "platform": platform.system().lower(),
        "python": platform.python_version(),
        "source_revision": _git_revision(root),
        "source_digest_sha256": source_digest,
    }
    _write_report_files(report_dir, run_id, results, metadata)
    overall = _overall_status(results)
    for result in results:
        print(f"[{result.status}] {result.check_id}: {result.summary}")
    try:
        display_path = report_dir.relative_to(root)
    except ValueError:
        display_path = report_dir
    print(f"verification {overall}: {display_path}")
    return _verification_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
