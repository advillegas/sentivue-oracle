from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from verification.verify import (
    FAIL,
    PASS,
    SKIP,
    CheckResult,
    CommandEvidence,
    check_bash,
    check_conductor_tests,
    check_config_formats,
    check_docs_commands,
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
        "# name | repo | include | slot | ctx | flags\n"
        "chat | example/chat | *.gguf | fast | 32768 | --temp 0.7\n"
        "embed | example/embed | *.gguf | embed | 8192 |\n",
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


def test_powershell_gate_rejects_non_ascii_and_parses_ast(tmp_path: Path) -> None:
    put(tmp_path, "scripts/good.ps1", "$value = 1\n")
    put(tmp_path, "scripts/non-ascii.ps1", "Write-Host 'caf\u00e9'\n")
    assert check_powershell(tmp_path).status == FAIL

    (tmp_path / "scripts/non-ascii.ps1").unlink()
    result = check_powershell(tmp_path)
    assert result.status in {PASS, SKIP}
    if powershell_executable():
        assert result.status == PASS
        assert result.commands and result.commands[0].exit_code == 0


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


def test_secret_gate_rejects_known_token_shapes(tmp_path: Path) -> None:
    source = put(tmp_path, "src/settings.txt", "token=ghp_" + ("A" * 36) + "\n")
    assert check_secrets(tmp_path).status == FAIL

    source.write_bytes(b"token=${GITHUB_TOKEN}\n")
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


def test_checkpoint_twin_commits_from_a_path_with_spaces(tmp_path: Path) -> None:
    checkpoint = REPO_ROOT / "bin/checkpoint.ps1"
    assert checkpoint.is_file()
    powershell = powershell_executable()
    if not powershell:
        pytest.skip("PowerShell is unavailable on this platform")

    repo = tmp_path / "checkpoint repository with spaces"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Verifier Fixture"],
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
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    subject = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert subject == "fixture checkpoint"
    ledger = (repo / "memory/LEDGER.md").read_text(encoding="utf-8")
    assert "fixture checkpoint" in ledger
