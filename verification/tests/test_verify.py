from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import verification.verify as verify
from verification.verify import (
    FAIL,
    PASS,
    PROVISIONAL,
    SKIP,
    CheckResult,
    CommandEvidence,
    check_bash,
    check_conductor_tests,
    check_config_formats,
    check_docs_commands,
    check_dependency_provenance,
    check_host_generated,
    check_line_policy,
    check_model_integrity,
    check_oversized,
    check_package_allowlist,
    check_path_safety,
    check_platform_twins,
    check_powershell,
    check_python,
    check_secrets,
    make_run_id,
    write_reports,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def put(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.write_bytes(data)
    return path


def add_line_policy(root: Path) -> None:
    put(
        root,
        ".gitattributes",
        "* text=auto eol=lf\n"
        "*.png binary\n"
        "*.jpg binary\n"
        "*.jpeg binary\n"
        "*.gif binary\n"
        "*.ico binary\n"
        "*.pdf binary\n"
        "*.zip binary\n"
        "*.gz binary\n",
    )
    put(
        root,
        ".editorconfig",
        "root = true\n"
        "\n"
        "[*]\n"
        "charset = utf-8\n"
        "end_of_line = lf\n"
        "insert_final_newline = true\n"
        "trim_trailing_whitespace = true\n",
    )


def valid_models(root: Path) -> None:
    put(
        root,
        "serving/models.manifest",
        "# name | repo | include | slot | ctx | flags | revision\n"
        "chat | example/chat | *.gguf | fast | 32768 | --temp 0.7 | dynamic\n"
        "embed | example/embed | *.gguf | embed | 8192 | | "
        + ("a" * 40)
        + "\n",
    )
    put(
        root,
        "serving/profiles.conf",
        "# name | memory | models | opus | sonnet | haiku | size\n"
        "lite | 8 | chat,embed | chat | chat | chat | ~10 GB\n",
    )


def powershell_executable() -> str | None:
    for candidate in ("powershell", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    fixed = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return str(fixed) if fixed.exists() else None


def bash_executable() -> str | None:
    if os.name == "nt":
        fixed = Path("C:/Program Files/Git/bin/bash.exe")
        return str(fixed) if fixed.exists() else None
    return shutil.which("bash")


def isolated_checkpoint_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    git_context = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    for name in list(env):
        if name.upper() in git_context or name.upper() == "ORACLE_ROOT":
            env.pop(name)
    env["ORACLE_ROOT"] = str(root)
    return env


def test_powershell_gate_rejects_non_ascii_and_parses_ast(tmp_path: Path) -> None:
    put(tmp_path, "scripts/good.ps1", "$value = 1\n")
    put(tmp_path, "scripts/non-ascii.ps1", "Write-Host 'caf\u00e9'\n")
    assert check_powershell(tmp_path).status == FAIL

    (tmp_path / "scripts/non-ascii.ps1").unlink()
    result = check_powershell(tmp_path)
    if powershell_executable():
        assert result.status == PASS
        assert result.commands and result.commands[0].exit_code == 0
    else:
        assert result.status == FAIL


def test_powershell_gate_rejects_malformed_ascii_syntax(tmp_path: Path) -> None:
    if not powershell_executable():
        pytest.skip("PowerShell is unavailable on this platform")

    put(tmp_path, "scripts/malformed.ps1", "$value = (\n")
    result = check_powershell(tmp_path)

    assert result.status == FAIL
    assert result.commands and result.commands[0].exit_code != 0
    assert any("malformed.ps1" in detail for detail in result.details)


def test_bash_gate_rejects_syntax_errors(tmp_path: Path) -> None:
    script = put(tmp_path, "scripts/check.sh", "#!/usr/bin/env bash\nif then\n")
    assert check_bash(tmp_path).status == FAIL

    script.write_bytes(b"#!/usr/bin/env bash\nprintf '%s\\n' ok\n")
    result = check_bash(tmp_path)
    assert result.status == PASS
    assert result.commands and result.commands[0].exit_code == 0


def test_bash_entrypoint_selects_a_working_python() -> None:
    bash = bash_executable()
    if not bash:
        pytest.skip("Bash is unavailable on this platform")
    completed = subprocess.run(
        [bash, str(REPO_ROOT / "verification/verify.sh"), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "Run read-only SentiVue Oracle verification." in completed.stdout


def test_powershell_entrypoint_probes_python_then_uses_py_launcher(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows command shims are required for this test")
    powershell = powershell_executable()
    if not powershell:
        pytest.skip("PowerShell is unavailable on this platform")

    verification_dir = tmp_path / "repository with spaces" / "verification"
    verification_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "verification/verify.ps1", verification_dir)
    put(
        verification_dir.parent,
        "verification/verify.py",
        "import sys\n"
        "assert '--static-only' in sys.argv\n"
        "print('PY-LAUNCHER-OK')\n",
    )
    fake_bin = tmp_path / "fake commands"
    fake_bin.mkdir()
    put(fake_bin, "python.cmd", "@echo off\r\nexit /b 49\r\n")
    put(
        fake_bin,
        "py.cmd",
        '@echo off\r\n"'
        + sys.executable
        + '" %2 %3 %4 %5 %6 %7 %8 %9\r\n',
    )
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(verification_dir / "verify.ps1"),
            "-StaticOnly",
        ],
        cwd=verification_dir.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "PY-LAUNCHER-OK" in completed.stdout


def test_required_shell_tools_cannot_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    put(tmp_path, "scripts/check.ps1", "$value = 1\n")
    put(tmp_path, "scripts/check.sh", "#!/usr/bin/env bash\ntrue\n")
    monkeypatch.setattr(verify, "_find_powershell", lambda: None)
    monkeypatch.setattr(verify, "_find_bash", lambda: None)

    assert check_powershell(tmp_path).status == FAIL
    assert check_bash(tmp_path).status == FAIL


def test_aggregate_skip_is_provisional_and_nonzero(tmp_path: Path) -> None:
    results = [CheckResult("required", SKIP, "not executed")]
    assert verify._overall_status(results) == PROVISIONAL
    assert verify._verification_exit_code(results) == 2
    report_dir = write_reports(tmp_path, "20260712T120000Z-cafefeed", results)
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["overall_status"] == PROVISIONAL
    assert verify._verification_exit_code(
        [CheckResult("failed", FAIL, "failed")]
    ) == 1
    assert verify._verification_exit_code([CheckResult("ok", PASS, "done")]) == 0


def test_crashed_check_preserves_explicit_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def crash(_root: Path) -> CheckResult:
        raise RuntimeError("fixture crash")

    monkeypatch.setattr(verify, "check_powershell", crash)
    for name in (
        "check_bash",
        "check_python",
        "check_conductor_tests",
        "check_line_policy",
        "check_model_integrity",
        "check_config_formats",
        "check_secrets",
        "check_host_generated",
        "check_oversized",
        "check_docs_commands",
        "check_dependency_provenance",
    ):
        monkeypatch.setattr(
            verify,
            name,
            lambda _root, check_id=name: CheckResult(check_id, PASS, "passed"),
        )
    monkeypatch.setattr(
        verify,
        "check_platform_twins",
        lambda _root, _policy: CheckResult("platform_twins", PASS, "passed"),
    )
    monkeypatch.setattr(
        verify,
        "check_path_safety",
        lambda _root, _scratch: CheckResult("path_safety", PASS, "passed"),
    )
    monkeypatch.setattr(
        verify,
        "check_package_allowlist",
        lambda _root, _policy: CheckResult("package_allowlist", PASS, "passed"),
    )

    results = verify._execute_static_checks(tmp_path, tmp_path / "report", {})
    assert results[0].check_id == "powershell"
    assert results[0].status == FAIL
    assert results[0].details == ["RuntimeError: fixture crash"]


def test_python_gate_rejects_compile_errors_without_source_cache(
    tmp_path: Path,
) -> None:
    source = put(tmp_path, "module.py", "def broken(:\n    pass\n")
    assert check_python(tmp_path).status == FAIL

    source.write_bytes(b"def working():\n    return 1\n")
    result = check_python(tmp_path)
    assert result.status == PASS
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


def test_conductor_gate_runs_pytest_and_preserves_exit_metadata(
    tmp_path: Path,
) -> None:
    test_file = put(
        tmp_path,
        "conductor/tests/test_sample.py",
        "def test_sample():\n    assert False\n",
    )
    failed = check_conductor_tests(tmp_path)
    assert failed.status == FAIL
    assert failed.commands[0].exit_code != 0

    test_file.write_bytes(b"def test_sample():\n    assert True\n")
    passed = check_conductor_tests(tmp_path)
    assert passed.status == PASS
    assert passed.commands[0].exit_code == 0


def test_line_policy_rejects_bom_and_crlf(tmp_path: Path) -> None:
    add_line_policy(tmp_path)
    source = put(tmp_path, "src/example.txt", "alpha\nbeta\n")
    assert check_line_policy(tmp_path).status == PASS

    source.write_bytes(b"\xef\xbb\xbfalpha\r\n")
    result = check_line_policy(tmp_path)
    assert result.status == FAIL
    assert any("BOM" in detail for detail in result.details)
    assert any("CRLF" in detail for detail in result.details)


def test_line_policy_rejects_utf16_and_non_utf8(tmp_path: Path) -> None:
    add_line_policy(tmp_path)
    source = put(tmp_path, "src/example.txt", "alpha\n".encode("utf-16"))
    result = check_line_policy(tmp_path)
    assert result.status == FAIL
    assert any("BOM" in detail or "UTF-16" in detail for detail in result.details)

    source.write_bytes(b"\x80\x81")
    result = check_line_policy(tmp_path)
    assert result.status == FAIL
    assert any("UTF-8" in detail for detail in result.details)


def test_line_policy_rejects_crlf_in_tracked_blob(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
        check=True,
    )
    source = put(tmp_path, "src/example.txt", b"alpha\r\nbeta\r\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src/example.txt"], check=True)
    add_line_policy(tmp_path)

    result = check_line_policy(tmp_path)
    assert result.status == FAIL
    assert any("tracked blob uses CRLF" in detail for detail in result.details)

    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "--renormalize", "src/example.txt"],
        check=True,
    )
    assert check_line_policy(tmp_path).status == PASS


def test_platform_twin_gate_requires_pair_or_documented_scope(
    tmp_path: Path,
) -> None:
    put(tmp_path, "tools/task.ps1", "Write-Host 'ok'\n")
    assert check_platform_twins(tmp_path, {}).status == FAIL

    policy = {
        "platform_scoped": [
            {
                "path": "tools/task.ps1",
                "platform": "windows",
                "reason": "Uses the Windows registry.",
            }
        ]
    }
    assert check_platform_twins(tmp_path, policy).status == PASS

    put(tmp_path, "tools/task.sh", "#!/usr/bin/env bash\ntrue\n")
    assert check_platform_twins(tmp_path, {}).status == PASS


def test_platform_twin_gate_inventories_extensionless_shebangs(
    tmp_path: Path,
) -> None:
    put(tmp_path, "bin/posix-tool", "#!/usr/bin/env bash\ntrue\n")
    assert check_platform_twins(tmp_path, {}).status == FAIL

    policy = {
        "platform_scoped": [
            {
                "path": "bin/posix-tool",
                "platform": "posix",
                "reason": "Uses a POSIX-only process interface.",
            }
        ]
    }
    assert check_platform_twins(tmp_path, policy).status == PASS


def test_repository_scopes_are_complete_and_reported_by_doctors() -> None:
    policy = json.loads(
        (REPO_ROOT / "verification/policy.json").read_text(encoding="utf-8")
    )
    scoped_paths = {entry["path"] for entry in policy["platform_scoped"]}
    assert {
        "bin/envoy-discover",
        "bin/envoy-fetch",
        "bin/oracle-menu",
        "install",
    } <= scoped_paths

    for relative in ("bootstrap/doctor.ps1", "bootstrap/doctor.sh"):
        doctor = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "verification/policy.json" in doctor.replace("\\", "/")
        assert "platform_scoped" in doctor
        assert "platform scope:" in doctor


def test_model_integrity_rejects_unknown_profile_and_tier_models(
    tmp_path: Path,
) -> None:
    valid_models(tmp_path)
    assert check_model_integrity(tmp_path).status == PASS

    put(
        tmp_path,
        "serving/profiles.conf",
        "bad | 8 | missing,embed | missing | missing | missing | ~1 GB\n",
    )
    result = check_model_integrity(tmp_path)
    assert result.status == FAIL
    assert any("missing" in detail for detail in result.details)


def test_model_integrity_requires_explicit_reproducibility_revision(
    tmp_path: Path,
) -> None:
    valid_models(tmp_path)
    manifest = tmp_path / "serving/models.manifest"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "example/chat | *.gguf | fast | 32768 | --temp 0.7 | dynamic",
            "example/chat | *.gguf | fast | 32768 | --temp 0.7 | main",
        ),
        encoding="utf-8",
    )

    result = check_model_integrity(tmp_path)

    assert result.status == FAIL
    assert any("revision" in detail for detail in result.details)


def test_dependency_provenance_gate_requires_central_exact_or_declared_dynamic_pins(
    tmp_path: Path,
) -> None:
    put(
        tmp_path,
        "VERSIONS.lock",
        "EXACT_TOOL_VERSION=v1.2.3\nDYNAMIC_IDE_VERSION=dynamic\n",
    )
    put(
        tmp_path,
        "serving/models.manifest",
        "chat | example/chat | model.gguf | fast | 32768 | | dynamic\n",
    )
    put(
        tmp_path,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "exact-tool",
                        "version_key": "EXACT_TOOL_VERSION",
                        "allow_dynamic": False,
                    },
                    {
                        "id": "dynamic-ide",
                        "version_key": "DYNAMIC_IDE_VERSION",
                        "allow_dynamic": True,
                    },
                ]
            }
        )
        + "\n",
    )
    assert check_dependency_provenance(tmp_path).status == PASS

    put(
        tmp_path,
        "env/pyproject.toml",
        '[project]\nname = "fixture"\nversion = "1.0.0"\n',
    )
    result = check_dependency_provenance(tmp_path)
    assert result.status == FAIL
    assert any("uv.lock" in detail for detail in result.details)
    put(tmp_path, "env/uv.lock", "version = 1\nrevision = 3\n")
    assert check_dependency_provenance(tmp_path).status == PASS

    versions = tmp_path / "VERSIONS.lock"
    versions.write_text(
        versions.read_text(encoding="utf-8")
        + "UNDECLARED_TOOL_VERSION=9.9.9\n",
        encoding="utf-8",
    )
    result = check_dependency_provenance(tmp_path)
    assert result.status == FAIL
    assert any("UNDECLARED_TOOL_VERSION" in detail for detail in result.details)

    versions.write_text(
        versions.read_text(encoding="utf-8")
        .replace("UNDECLARED_TOOL_VERSION=9.9.9\n", "")
        .replace("v1.2.3", "latest"),
        encoding="utf-8",
    )
    result = check_dependency_provenance(tmp_path)

    assert result.status == FAIL
    assert any("EXACT_TOOL_VERSION" in detail for detail in result.details)


@pytest.mark.parametrize(
    ("suffix", "valid", "invalid"),
    [
        (".json", '{"path": "folder with spaces/file"}\n', '{"path": }\n'),
        (
            ".jsonc",
            '// comment\n{"url": "http://localhost/a", "items": [1, 2,],}\n',
            '{"items": [1,, 2]}\n',
        ),
        (".toml", 'path = "folder with spaces/file"\n', "path = [\n"),
        (
            ".yaml",
            "root:\n  path: folder with spaces/file\n  enabled: true\n",
            "root:\n  child: ok\n bad: indentation\n",
        ),
        (".plist", plistlib.dumps({"path": "folder with spaces/file"}), b"<plist>"),
    ],
)
def test_config_gate_parses_supported_formats(
    tmp_path: Path, suffix: str, valid: str | bytes, invalid: str | bytes
) -> None:
    config = put(tmp_path, f"config/settings{suffix}", invalid)
    assert check_config_formats(tmp_path).status == FAIL

    data = valid if isinstance(valid, bytes) else valid.encode("utf-8")
    config.write_bytes(data)
    assert check_config_formats(tmp_path).status == PASS


@pytest.mark.parametrize(
    "invalid",
    [
        "root: [1,, 2]\n",
        "root: {first: 1,, second: 2}\n",
    ],
)
def test_yaml_gate_rejects_malformed_flow_values(
    tmp_path: Path, invalid: str
) -> None:
    put(tmp_path, "config/settings.yaml", invalid)
    assert check_config_formats(tmp_path).status == FAIL


def test_path_safety_gate_uses_argv_and_generated_config(tmp_path: Path) -> None:
    root = tmp_path / "repository with spaces"
    root.mkdir()
    scratch = tmp_path / "evidence with spaces"
    result = check_path_safety(root, scratch)
    assert result.status == PASS
    assert result.commands and result.commands[0].exit_code == 0
    assert result.commands[0].argv[0] == sys.executable


def test_package_allowlist_rejects_unexpected_roots(tmp_path: Path) -> None:
    put(tmp_path, "allowed/source.txt", "ok\n")
    put(tmp_path, "unexpected/source.txt", "no\n")
    policy = {"package_allowlist": {"roots": ["allowed"], "files": []}}
    assert check_package_allowlist(tmp_path, policy).status == FAIL

    policy["package_allowlist"]["roots"].append("unexpected")
    assert check_package_allowlist(tmp_path, policy).status == PASS


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("bin/tool.exe", b"MZ\x00fixture"),
        ("models/tiny.gguf", b"GGUF\x00fixture"),
        ("toolchains/compiler", b"\x7fELF\x00fixture"),
        ("bin/toolchain.zip", b"PK\x03\x04fixture"),
    ],
)
def test_package_allowlist_rejects_prohibited_artifacts_below_size_limit(
    tmp_path: Path, relative: str, content: bytes
) -> None:
    put(tmp_path, "assets/logo.png", b"\x89PNG\r\n\x1a\n\x00source asset")
    artifact = put(tmp_path, relative, content)
    policy = {
        "package_allowlist": {
            "roots": ["assets", "bin", "models", "toolchains"],
            "files": [],
            "source_assets": ["assets/logo.png", relative],
        }
    }

    result = check_package_allowlist(tmp_path, policy)

    assert artifact.stat().st_size < verify.MAX_SOURCE_BYTES
    assert result.status == FAIL
    assert any(relative in detail for detail in result.details)

    artifact.unlink()
    assert check_package_allowlist(tmp_path, policy).status == PASS


def test_secret_gate_rejects_known_token_shapes(tmp_path: Path) -> None:
    source = put(
        tmp_path,
        "src/settings.txt",
        "token=ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8\n",
    )
    assert check_secrets(tmp_path).status == FAIL

    for placeholder in (
        "${GITHUB_TOKEN}",
        "ghp_" + ("A" * 36),
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    ):
        source.write_text(f"token={placeholder}\n", encoding="utf-8")
        assert check_secrets(tmp_path).status == PASS


def test_secret_gate_rejects_hugging_face_tokens_without_placeholder_false_positives(
    tmp_path: Path,
) -> None:
    source = put(
        tmp_path,
        "docs/auth.md",
        "token=hf_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8\n",
    )
    result = check_secrets(tmp_path)
    assert result.status == FAIL
    assert any("Hugging Face" in detail for detail in result.details)

    for placeholder in (
        "hf_your_token_here",
        "hf_" + ("x" * 34),
        "hf_abcdefghijklmnopqrstuvwxyz12345678",
    ):
        source.write_text(f"token={placeholder}\n", encoding="utf-8")
        assert check_secrets(tmp_path).status == PASS


@pytest.mark.parametrize(
    ("label", "token", "placeholder", "example"),
    [
        (
            "Anthropic",
            "sk-ant-" + "api03-" + ("a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" * 2),
            "sk-ant-" + "api03-" + ("x" * 64),
            "sk-ant-your-key-here",
        ),
        (
            "npm",
            "npm_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
            "npm_" + ("x" * 36),
            "npm_your_token_here",
        ),
        (
            "PyPI",
            "pypi-"
            + "AgEIcHlwaS5vcmcC"
            + ("a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" * 2),
            "pypi-" + "AgEIcHlwaS5vcmcC" + ("x" * 40),
            "pypi-your-token-here",
        ),
    ],
)
def test_secret_gate_rejects_package_service_tokens_without_example_false_positives(
    tmp_path: Path,
    label: str,
    token: str,
    placeholder: str,
    example: str,
) -> None:
    source = put(tmp_path, "docs/package-auth.md", f"token={token}\n")

    result = check_secrets(tmp_path)

    assert result.status == FAIL
    assert any(label in detail for detail in result.details)

    for allowed in (placeholder, example):
        source.write_text(f"token={allowed}\n", encoding="utf-8")
        assert check_secrets(tmp_path).status == PASS


def test_host_generated_gate_rejects_generated_files(tmp_path: Path) -> None:
    generated = put(tmp_path, "src/.DS_Store", b"host state")
    assert check_host_generated(tmp_path).status == FAIL

    generated.unlink()
    put(tmp_path, "src/source.py", "value = 1\n")
    assert check_host_generated(tmp_path).status == PASS


def test_oversized_gate_rejects_files_above_limit(tmp_path: Path) -> None:
    source = put(tmp_path, "src/data.bin", b"123456789")
    assert check_oversized(tmp_path, max_bytes=8).status == FAIL

    source.write_bytes(b"12345678")
    assert check_oversized(tmp_path, max_bytes=8).status == FAIL

    source.write_bytes(b"1234567")
    assert check_oversized(tmp_path, max_bytes=8).status == PASS


def test_documentation_gate_resolves_repository_commands(tmp_path: Path) -> None:
    readme = put(
        tmp_path,
        "README.md",
        "Run `bash bootstrap/missing.sh` to verify the install.\n",
    )
    assert check_docs_commands(tmp_path).status == FAIL

    put(tmp_path, "bootstrap/missing.sh", "#!/usr/bin/env bash\ntrue\n")
    assert check_docs_commands(tmp_path).status == PASS

    readme.write_bytes(
        b"Run `powershell -File bootstrap\\missing.ps1` to verify the install.\n"
    )
    assert check_docs_commands(tmp_path).status == FAIL


@pytest.mark.parametrize(
    ("reference", "candidate"),
    [
        ("bin/missing-tool", "bin/missing-tool"),
        ("bin/missing-tool.rb", "bin/missing-tool.rb"),
        ("./bin/missing-tool", "bin/missing-tool"),
    ],
)
def test_documentation_gate_resolves_general_local_entrypoints(
    tmp_path: Path, reference: str, candidate: str
) -> None:
    put(tmp_path, "README.md", f"Run `{reference}` to inspect the service.\n")

    result = check_docs_commands(tmp_path)

    assert result.status == FAIL
    assert any(candidate in detail for detail in result.details)

    put(tmp_path, candidate, "#!/usr/bin/env bash\ntrue\n")
    assert check_docs_commands(tmp_path).status == PASS


def test_documentation_gate_ignores_noncommand_local_path_references(
    tmp_path: Path,
) -> None:
    put(
        tmp_path,
        "README.md",
        "`serving/tiers.env` is generated on the target machine.\n"
        "Queued follow-up: `bin/future-tool`.\n"
        "Sources are installed under `harness/tool/vendor/`.\n",
    )

    assert check_docs_commands(tmp_path).status == PASS


@pytest.mark.parametrize(
    ("language", "command", "candidate"),
    [
        ("bash", "bin/missing-tool --help", "bin/missing-tool"),
        (
            "powershell",
            r"& .\bin\missing-tool.ps1 -Help",
            "bin/missing-tool.ps1",
        ),
    ],
)
def test_documentation_gate_resolves_bare_local_commands_in_shell_fences(
    tmp_path: Path, language: str, command: str, candidate: str
) -> None:
    put(
        tmp_path,
        "README.md",
        "Prose output mentions bin/prose-only --help.\n"
        "```text\n"
        "bin/output-only --help\n"
        "```\n"
        f"```{language}\n"
        f"{command}\n"
        "```\n",
    )

    result = check_docs_commands(tmp_path)

    assert result.status == FAIL
    assert any(candidate in detail for detail in result.details)
    assert not any("prose-only" in detail for detail in result.details)
    assert not any("output-only" in detail for detail in result.details)

    put(tmp_path, candidate, "#!/usr/bin/env bash\ntrue\n")
    assert check_docs_commands(tmp_path).status == PASS


def test_run_ids_are_unique_and_portable() -> None:
    first = make_run_id()
    second = make_run_id()
    assert first != second
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", first)


def test_reports_are_machine_readable_human_readable_and_non_overwriting(
    tmp_path: Path,
) -> None:
    report_base = tmp_path / "reports with spaces"
    results = [
        CheckResult(
            "fixture",
            PASS,
            "fixture passed",
            commands=[
                CommandEvidence(
                    [sys.executable, "path with spaces/check.py"],
                    0,
                    stdout=str(tmp_path),
                    cwd=str(tmp_path),
                )
            ],
        )
    ]
    report_dir = write_reports(
        report_base,
        "20260712T120000Z-deadbeef",
        results,
        {"mode": "static"},
    )
    report_json = report_dir / "report.json"
    summary = report_dir / "summary.txt"
    assert report_json.is_file()
    assert summary.is_file()

    payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert payload["run_id"] == "20260712T120000Z-deadbeef"
    assert payload["overall_status"] == PASS
    assert payload["checks"][0]["commands"][0]["exit_code"] == 0
    assert str(tmp_path) not in report_json.read_text(encoding="utf-8")
    assert "[PASS] fixture" in summary.read_text(encoding="utf-8")

    original = report_json.read_bytes()
    with pytest.raises(FileExistsError):
        write_reports(report_base, report_dir.name, results)
    assert report_json.read_bytes() == original


def test_checksum_manifest_covers_all_retained_evidence_artifacts(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "verification-run"
    report_dir.mkdir()
    put(
        report_dir,
        "artifacts/path with spaces/generated config.json",
        '{"path": "model with spaces.gguf"}\n',
    )
    put(
        report_dir,
        "artifacts/path with spaces/command helper.py",
        "print('ok')\n",
    )

    verify._write_report_files(
        report_dir,
        "20260712T120000Z-a11ce123",
        [CheckResult("fixture", PASS, "passed")],
        {"mode": "static"},
    )

    entries = []
    for line in (report_dir / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, relative = line.split("  ", 1)
        entries.append((relative, digest))
    expected = sorted(
        path.relative_to(report_dir).as_posix()
        for path in report_dir.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    assert [relative for relative, _digest in entries] == expected
    for relative, digest in entries:
        assert digest == hashlib.sha256((report_dir / relative).read_bytes()).hexdigest()


def test_cli_rejects_report_root_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    outside = tmp_path / "outside evidence"
    monkeypatch.setattr(
        verify,
        "_execute_static_checks",
        lambda *_args: [CheckResult("fixture", PASS, "passed")],
    )
    monkeypatch.setattr(verify, "_load_policy", lambda _root: {})
    monkeypatch.setattr(verify, "_source_digest", lambda _root: "0" * 64)
    monkeypatch.setattr(verify, "_git_revision", lambda _root: "fixture")

    with pytest.raises(SystemExit) as exc:
        verify.main(
            [
                "--root",
                str(root),
                "--report-root",
                str(outside),
                "--run-id",
                "20260712T120000Z-feedface",
            ]
        )
    assert exc.value.code == 2
    assert not outside.exists()
    assert "ReportRoot" not in (
        REPO_ROOT / "verification/verify.ps1"
    ).read_text(encoding="utf-8")


def test_repository_policy_entrypoints_and_ci_are_present() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/.superpowers/" in gitignore
    assert (REPO_ROOT / ".gitattributes").is_file()
    assert (REPO_ROOT / ".editorconfig").is_file()
    assert (REPO_ROOT / "verification/policy.json").is_file()
    assert (REPO_ROOT / "verification/verify.ps1").is_file()
    assert (REPO_ROOT / "verification/verify.sh").is_file()
    assert (REPO_ROOT / "bin/checkpoint.ps1").is_file()

    workflow = (REPO_ROOT / ".github/workflows/verify.yml").read_text(
        encoding="utf-8"
    )
    assert "windows-latest" in workflow
    assert "macos-latest" in workflow
    assert "verification/verify.ps1 -StaticOnly" in workflow
    assert "bash verification/verify.sh --static-only" in workflow
    assert workflow.count("python -m pytest verification/tests -q") == 2


def test_checkpoint_twin_commits_from_a_path_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint = REPO_ROOT / "bin/checkpoint.ps1"
    assert checkpoint.is_file()
    powershell = powershell_executable()
    if not powershell:
        pytest.skip("PowerShell is unavailable on this platform")

    other_checkout = tmp_path / "other checkout"
    other_checkout.mkdir()
    monkeypatch.setenv("ORACLE_ROOT", str(other_checkout))

    repo = tmp_path / "checkpoint repository with spaces"
    repo.mkdir()
    env = isolated_checkpoint_env(repo)
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Verifier Fixture"],
        env=env,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "verifier@example.invalid",
        ],
        env=env,
        check=True,
    )
    copied = repo / "bin/checkpoint.ps1"
    copied.parent.mkdir()
    shutil.copy2(checkpoint, copied)
    put(repo, "source file.txt", "evidence\n")

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copied),
            "fixture checkpoint",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    subject = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert subject == "fixture checkpoint"
    ledger = (repo / "memory/LEDGER.md").read_text(encoding="utf-8")
    assert "fixture checkpoint" in ledger
    assert not (other_checkout / "memory/LEDGER.md").exists()


@pytest.mark.parametrize("entrypoint", ["powershell", "bash"])
def test_checkpoint_twins_reject_exactly_50_mib(
    tmp_path: Path, entrypoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if entrypoint == "powershell" and not powershell_executable():
        pytest.skip("PowerShell is unavailable on this platform")
    if entrypoint == "bash" and not bash_executable():
        pytest.skip("Bash is unavailable on this platform")

    repo = tmp_path / f"{entrypoint} boundary repository"
    repo.mkdir()
    env = isolated_checkpoint_env(repo)
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Verifier Fixture"],
        env=env,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "verifier@example.invalid",
        ],
        env=env,
        check=True,
    )
    source_entrypoint = (
        REPO_ROOT / "bin/checkpoint.ps1"
        if entrypoint == "powershell"
        else REPO_ROOT / "bin/checkpoint"
    )
    copied = repo / "bin" / source_entrypoint.name
    copied.parent.mkdir()
    shutil.copy2(source_entrypoint, copied)
    boundary = repo / "exactly-50-mib.bin"
    with boundary.open("wb") as handle:
        handle.seek((50 * 1024 * 1024) - 1)
        handle.write(b"\0")

    other_checkout = tmp_path / "other checkout"
    other_checkout.mkdir()
    monkeypatch.setenv("ORACLE_ROOT", str(other_checkout))
    monkeypatch.setenv("GIT_DIR", str(other_checkout / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other_checkout))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other_checkout / "index"))

    if entrypoint == "powershell":
        command = [
            powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copied),
            "must be rejected",
        ]
    else:
        command = [bash_executable(), str(copied), "must be rejected"]
    completed = subprocess.run(
        command,
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "REFUSED" in completed.stdout + completed.stderr
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        env=env,
        capture_output=True,
        check=False,
    )
    assert head.returncode != 0
