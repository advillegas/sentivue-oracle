from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path

import pytest

import verification.lifecycle as lifecycle
from verification.lifecycle import (
    LifecycleError,
    atomic_write_text,
    begin_install_phase,
    build_source_archives,
    export_artifact,
    initialize_install_state,
    mark_install_phase,
    phase_is_current,
    publish_release,
    record_cached_artifact,
    record_model_snapshot,
    register_owned_path,
    resolve_cached_artifact,
    sync_model_configs,
    uninstall,
    validate_artifact_manifest,
    validate_dependency_inputs,
    validate_model_snapshot,
    verify_release_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def put(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.write_bytes(data)
    return path


def one_click_payloads(
    extra_files: dict[str, bytes] | None = None,
) -> tuple[bytes, bytes, dict[str, bytes]]:
    revision = "a" * 40
    source_files = {
        "README.md": b"fixture\n",
        "install": b"#!/bin/bash\nexit 0\n",
        "bin/oracle.ps1": b"exit 0\n",
        "bootstrap/build-installers.ps1": b"# protected fixture\n",
        "serving/profiles.conf": (
            b"full|1|fixture|fixture|fixture|fixture|0 GB\n"
            b"micro|0|fixture|fixture|fixture|fixture|0 GB\n"
        ),
        "serving/tiers.env": (
            b"OPUS_MODEL=fixture\nSONNET_MODEL=fixture\nHAIKU_MODEL=fixture\n"
        ),
        "verification/lifecycle.py": b"# lifecycle fixture\n",
    }
    source_files.update(extra_files or {})
    records = [
        {
            "path": relative,
            "mode": "100755" if relative == "install" else "100644",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        for relative, data in sorted(source_files.items())
    ]
    files = dict(source_files)
    files["ARTIFACTS.json"] = (
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": revision,
                "requirements": [],
                "note": "fixture",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    files["SOURCE-PROVENANCE.json"] = (
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": revision,
                "files": records,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as archive:
        for relative, data in sorted(files.items()):
            info = tarfile.TarInfo(f"sentivue-oracle/{relative}")
            info.size = len(data)
            info.mode = 0o755 if relative == "install" else 0o644
            archive.addfile(info, io.BytesIO(data))
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, data in sorted(files.items()):
            info = zipfile.ZipInfo(f"sentivue-oracle/{relative}")
            info.create_system = 3
            mode = 0o755 if relative == "install" else 0o644
            info.external_attr = (mode | 0o100000) << 16
            archive.writestr(info, data)
    return tar_buffer.getvalue(), zip_buffer.getvalue(), files


def init_repository(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Lifecycle Fixture"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "config",
            "user.email",
            "lifecycle@example.invalid",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True
    )
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def add_package_policy(root: Path, roots: list[str]) -> None:
    allowed_roots = sorted({*roots, "verification"})
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "schema_version": 1,
                "package_allowlist": {
                    "roots": allowed_roots,
                    "files": ["README.md"],
                    "source_assets": [],
                },
            },
            indent=2,
        )
        + "\n",
    )


def archive_names(path: Path) -> set[str]:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path, "r:gz") as archive:
        return {member.name for member in archive.getmembers() if member.isfile()}


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape",
        "safe/../../escape",
        "/absolute",
        r"C:\absolute",
        r"safe\..\escape",
        "safe/\x00escape",
        "safe/\ncontrol",
    ],
)
def test_archive_path_validation_rejects_traversal_and_control_names(
    unsafe: str,
) -> None:
    with pytest.raises(LifecycleError):
        lifecycle.validate_relative_path(unsafe)

    assert lifecycle.validate_relative_path("folder with spaces/source.txt") == (
        "folder with spaces/source.txt"
    )


def test_package_uses_immutable_revision_allowlist_and_hard_exclusions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository with spaces"
    root.mkdir()
    add_package_policy(
        root,
        [
            "src",
            "memory",
            "logs",
            "reports",
            "models",
            "toolchains",
            "incoming",
            "data",
        ],
    )
    put(root, "README.md", "fixture\n")
    put(root, "src/runtime source.py", "VALUE = 'committed'\n")
    put(root, "memory/sessions/live.md", "runtime memory\n")
    put(root, "logs/run.log", "runtime log\n")
    put(root, "reports/audit.txt", "runtime report\n")
    put(root, "models/model.gguf", b"GGUF fixture")
    put(root, "toolchains/compiler.exe", b"MZ fixture")
    put(root, "incoming/quarantine.txt", "quarantine\n")
    put(root, "data/live.sqlite", b"SQLite format 3\0")
    revision = init_repository(root)

    put(root, "src/runtime source.py", "VALUE = 'dirty'\n")
    put(root, "src/untracked-secret.txt", "ghp_not-packaged\n")
    output = tmp_path / "release output with spaces"
    bundle = build_source_archives(root, revision, output, "v1.2.3")

    assert bundle.revision == revision
    assert len(bundle.archives) == 2
    for archive in bundle.archives:
        names = archive_names(archive)
        assert "sentivue-oracle/src/runtime source.py" in names
        assert "sentivue-oracle/ARTIFACTS.json" in names
        assert "sentivue-oracle/SOURCE-PROVENANCE.json" in names
        assert not any("untracked-secret" in name for name in names)
        blocked_parts = (
            "/memory/",
            "/logs/",
            "/reports/",
            "/models/",
            "/toolchains/",
            "/incoming/",
            "/data/",
            "/.git/",
        )
        assert not any(blocked in name for name in names for blocked in blocked_parts)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zipped:
                assert (
                    zipped.read("sentivue-oracle/src/runtime source.py")
                    == b"VALUE = 'committed'\n"
                )
        else:
            with tarfile.open(archive, "r:gz") as tarred:
                stream = tarred.extractfile("sentivue-oracle/src/runtime source.py")
                assert stream is not None
                assert stream.read() == b"VALUE = 'committed'\n"

    verify_release_bundle(output)


def test_full_offline_installer_payload_includes_only_validated_dependency_export(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    dependency = put(
        tmp_path,
        "downloads/tool.bin",
        b"\x00sidecar-only-dependency-fixture\xff",
    )
    digest = hashlib.sha256(dependency.read_bytes()).hexdigest()
    put(
        root,
        "VERSIONS.lock",
        f"TOOL_VERSION=1.2.3\nTOOL_SHA256={digest}\n",
    )
    put(root, "README.md", "fixture\n")
    put(root, "serving/models.manifest", "# fixture\n")
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "schema_version": 1,
                "package_allowlist": {
                    "roots": ["serving", "verification"],
                    "files": ["README.md", "VERSIONS.lock"],
                    "source_assets": [],
                },
                "dependency_inputs": [
                    {
                        "id": "tool",
                        "kind": "native",
                        "version_key": "TOOL_VERSION",
                        "allow_dynamic": False,
                        "optional": False,
                        "platforms": ["windows", "macos"],
                        "source": {
                            "identity": dependency.as_uri(),
                            "digest_key": "TOOL_SHA256",
                        },
                    }
                ],
            },
            indent=2,
        )
        + "\n",
    )
    cache = tmp_path / "validated dependency export"
    record = record_cached_artifact(
        cache,
        artifact_id="tool",
        source_file=dependency,
        source_url=dependency.as_uri(),
        requested_version="1.2.3",
        resolved_version="1.2.3",
        trust="policy-bound",
    )

    entries, artifacts = lifecycle._validated_dependency_cache_entries(
        root,
        cache,
    )

    by_path = {entry.path: entry.data for entry in entries}
    embedded_path = f"incoming/dependency-cache/{record.cache_path}"
    assert by_path[embedded_path] == dependency.read_bytes()
    assert "incoming/dependency-cache/manifest.json" in by_path
    assert artifacts == {
        "artifact_ids": ["tool"],
        "embedded": True,
        "manifest_path": "incoming/dependency-cache/manifest.json",
        "manifest_sha256": hashlib.sha256(
            by_path["incoming/dependency-cache/manifest.json"]
        ).hexdigest(),
        "models_embedded": False,
    }
    sidecar = tmp_path / "dependencies.zip"
    lifecycle._write_zip(sidecar, entries, 1_700_000_000)
    with zipfile.ZipFile(sidecar) as archive:
        assert archive.read(f"sentivue-oracle/{embedded_path}") == dependency.read_bytes()
    sidecar_installer = lifecycle._windows_installer_text(
        one_click_payloads()[1],
        hashlib.sha256(one_click_payloads()[1]).hexdigest(),
        dependency_bundle_name=sidecar.name,
        dependency_bundle_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    ).encode("ascii")
    assert dependency.read_bytes() not in sidecar_installer
    assert b"$dependencyStream = [IO.File]::Open(" in sidecar_installer
    assert b"[Convert]::FromBase64String($encoded)" in sidecar_installer

    (cache / record.cache_path).write_bytes(b"tampered")
    with pytest.raises(LifecycleError, match="full-offline dependency export"):
        lifecycle._validated_dependency_cache_entries(
            root,
            cache,
        )


def test_final_release_manifest_binds_base_bundle_and_macos_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    add_package_policy(root, ["bootstrap", "src"])
    put(root, "README.md", "fixture\n")
    put(root, "src/main.py", "VALUE = 1\n")
    immutable_package_builder = put(
        root,
        "bootstrap/build-macos-package.sh",
        "#!/bin/bash\n# immutable package builder\n",
    )
    revision = init_repository(root)
    base = build_source_archives(root, revision, tmp_path / "base", "v1.2.3")
    command_installer = put(
        base.output_dir,
        "SentiVue-Oracle-Installer-v1.2.3.command",
        b"command installer",
    )
    monkeypatch.setattr(lifecycle, "verify_release_bundle", lambda *_a: base)
    base_payload = json.loads(base.provenance.read_text(encoding="utf-8"))
    package_dir = tmp_path / "package"
    package = put(
        package_dir,
        "SentiVue-Oracle-Source-Installer-v1.2.3.pkg",
        b"package bytes",
    )
    package_provenance = put(
        package_dir,
        f"{package.name}.provenance.json",
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1.2.3",
                "source_revision": revision,
                "artifact": package.name,
                "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                "base_provenance_sha256": hashlib.sha256(
                    base.provenance.read_bytes()
                ).hexdigest(),
                "base_checksums_sha256": hashlib.sha256(
                    base.checksums.read_bytes()
                ).hexdigest(),
                "base_builder_sha256": base_payload["builder"]["sha256"],
                "package_builder_sha256": hashlib.sha256(
                    immutable_package_builder.read_bytes()
                ).hexdigest(),
                "embedded_installer": command_installer.name,
                "embedded_installer_sha256": hashlib.sha256(
                    command_installer.read_bytes()
                ).hexdigest(),
                "dependency_bundle": "-",
                "dependency_bundle_sha256": "-",
                "code_signing": "unsigned",
                "notarization": "not-notarized",
            }
        )
        + "\n",
    )
    put(
        package_dir,
        f"{package.name}.sha256",
        f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n"
        f"{hashlib.sha256(package_provenance.read_bytes()).hexdigest()}  "
        f"{package_provenance.name}\n",
    )

    checksums, provenance = lifecycle.finalize_installer_release(
        base.output_dir,
        package_dir,
        tmp_path / "final",
    )

    final_payload = json.loads(provenance.read_text(encoding="utf-8"))
    final_names = {item["name"] for item in final_payload["artifacts"]}
    assert package.name in final_names
    assert "PROVENANCE.json" in final_names
    assert checksums.name == "RELEASE-SHA256SUMS"
    assert hashlib.sha256(package_provenance.read_bytes()).hexdigest() == (
        final_payload["package_provenance_sha256"]
    )

    package.write_bytes(b"substituted")
    with pytest.raises(LifecycleError, match="package provenance"):
        lifecycle.finalize_installer_release(
            base.output_dir,
            package_dir,
            tmp_path / "rejected",
        )


def test_package_rejects_tracked_source_outside_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    add_package_policy(root, ["src"])
    put(root, "README.md", "fixture\n")
    put(root, "src/main.py", "VALUE = 1\n")
    put(root, "surprise/payload.txt", "unexpected\n")
    revision = init_repository(root)

    with pytest.raises(LifecycleError, match="outside package allowlist"):
        build_source_archives(root, revision, tmp_path / "out", "v1.0.0")


def test_package_validates_dependency_policy_from_immutable_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=latest\n")
    put(
        root,
        "serving/models.manifest",
        "chat | repo | model.gguf | fast | 32768 | | dynamic\n",
    )
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "package_allowlist": {
                    "roots": ["serving", "verification"],
                    "files": ["VERSIONS.lock"],
                },
                "dependency_inputs": [
                    {
                        "id": "tool",
                        "version_key": "TOOL_VERSION",
                        "allow_dynamic": False,
                    }
                ],
            }
        )
        + "\n",
    )
    revision = init_repository(root)

    with pytest.raises(LifecycleError, match="TOOL_VERSION"):
        build_source_archives(root, revision, tmp_path / "out", "v1.2.3")


def test_release_bundle_checksums_and_provenance_detect_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    add_package_policy(root, ["src"])
    put(root, "README.md", "fixture\n")
    put(root, "src/main.py", "VALUE = 1\n")
    revision = init_repository(root)
    output = tmp_path / "out"

    bundle = build_source_archives(root, revision, output, "v1.0.0")
    provenance = json.loads(bundle.provenance.read_text(encoding="utf-8"))
    assert provenance["source_revision"] == revision
    assert {entry["name"] for entry in provenance["artifacts"]} == {
        path.name for path in bundle.archives
    }
    assert verify_release_bundle(output).revision == revision

    bundle.archives[0].write_bytes(bundle.archives[0].read_bytes() + b"tampered")
    with pytest.raises(LifecycleError, match="checksum"):
        verify_release_bundle(output)


@pytest.mark.parametrize(
    "entry",
    [
        "{digest}  asset.zip  ignored\n",
        "{digest} *asset.zip\n",
        "{digest}  asset.zip\r\n",
        "{digest}  asset.zip\n\n",
        "{upper}  asset.zip\n",
        "{digest}  ../asset.zip\n",
    ],
)
def test_checksum_manifest_parser_rejects_noncanonical_entries(
    tmp_path: Path,
    entry: str,
) -> None:
    digest = hashlib.sha256(b"fixture").hexdigest()
    manifest = put(
        tmp_path,
        "SHA256SUMS",
        entry.format(digest=digest, upper=digest.upper()),
    )

    with pytest.raises(LifecycleError, match="checksum"):
        lifecycle._read_checksum_manifest(manifest)


@pytest.mark.parametrize(
    "mutation",
    ["source-top-level", "artifact-top-level", "file-record"],
)
def test_installer_payload_rejects_unknown_manifest_fields(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    add_package_policy(root, ["src"])
    put(root, "README.md", "fixture\n")
    put(root, "src/main.py", "VALUE = 1\n")
    revision = init_repository(root)
    bundle = build_source_archives(root, revision, tmp_path / "out", "v1.0.0")
    source_zip = next(path for path in bundle.archives if path.suffix == ".zip")
    with zipfile.ZipFile(source_zip) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    prefix = f"{lifecycle.PRODUCT_PREFIX}/"
    source_name = prefix + "SOURCE-PROVENANCE.json"
    artifacts_name = prefix + "ARTIFACTS.json"
    if mutation == "source-top-level":
        payload = json.loads(files[source_name])
        payload["unexpected"] = True
        files[source_name] = lifecycle._json_bytes(payload)
    elif mutation == "artifact-top-level":
        payload = json.loads(files[artifacts_name])
        payload["unexpected"] = True
        files[artifacts_name] = lifecycle._json_bytes(payload)
    else:
        payload = json.loads(files[source_name])
        payload["files"][0]["unexpected"] = True
        files[source_name] = lifecycle._json_bytes(payload)
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)

    with pytest.raises(LifecycleError, match="manifest|provenance"):
        lifecycle._installer_payload_files(rebuilt.getvalue(), ".cmd")


def test_release_preflight_failure_runs_no_mutating_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise LifecycleError("fixture preflight failed")

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lifecycle, "preflight_release", fail_preflight)
    monkeypatch.setattr(lifecycle, "_run", fake_run)

    with pytest.raises(LifecycleError, match="preflight"):
        publish_release(tmp_path, "v1.2.3", tmp_path / "out")

    assert commands == []


def test_release_cli_defaults_to_preflight_and_requires_explicit_publish() -> None:
    parser = lifecycle.build_parser()
    common = [
        "release",
        "--root",
        ".",
        "--version",
        "v1.2.3",
        "--output",
        "out",
    ]

    assert parser.parse_args(common).publish is False
    assert parser.parse_args([*common, "--publish"]).publish is True


def test_dependency_validation_cli_can_exclude_separate_model_imports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def validate(*_args: object, **kwargs: object) -> list[str]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(lifecycle, "validate_dependency_inputs", validate)
    args = lifecycle.build_parser().parse_args(
        [
            "validate-dependencies",
            "--root",
            str(tmp_path),
            "--exclude-models",
        ]
    )

    assert args.handler(args) == 0
    assert captured["include_models"] is False


def test_operator_targets_preserve_safe_lifecycle_defaults() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    windows_installer = (
        REPO_ROOT / "bootstrap" / "build-one-click-installers.ps1"
    ).read_text(encoding="ascii")
    macos_installer = (
        REPO_ROOT / "bootstrap" / "build-one-click-installers.sh"
    ).read_text(encoding="ascii")

    assert 'bootstrap/package.sh --version "$(VERSION)"' in makefile
    assert 'bootstrap/build-one-click-installers.sh --version "$(VERSION)"' in makefile
    assert "$(CONFIRM_PURGE)" in makefile
    assert "--confirm-purge" in makefile
    assert "release.ps1 -Version vX.Y.Z -Publish" in readme
    assert "--dependency-cache" in windows_installer
    assert "--dependency-cache" in macos_installer
    assert "-DependencyCache <path>" in readme


def test_existing_release_version_is_immutable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    asset = put(output, "asset.zip", b"fixture")
    provenance = put(
        output,
        "PROVENANCE.json",
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1.2.3",
                "source_revision": "a" * 40,
                "artifacts": [
                    {
                        "name": asset.name,
                        "sha256": hashlib.sha256(b"fixture").hexdigest(),
                        "size": len(b"fixture"),
                    }
                ],
            }
        )
        + "\n",
    )
    put(
        output,
        "SHA256SUMS",
        hashlib.sha256(b"fixture").hexdigest()
        + "  asset.zip\n"
        + hashlib.sha256(provenance.read_bytes()).hexdigest()
        + "  PROVENANCE.json\n",
    )
    preflight = lifecycle.ReleaseBundle(
        version="v1.2.3",
        revision="a" * 40,
        output_dir=output,
        archives=[asset],
        checksums=output / "SHA256SUMS",
        provenance=output / "PROVENANCE.json",
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(lifecycle, "preflight_release", lambda *_a, **_k: preflight)
    monkeypatch.setattr(lifecycle, "verify_release_bundle", lambda *_a, **_k: preflight)

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, "c" * 40 + "\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)

    with pytest.raises(LifecycleError, match="different revision"):
        publish_release(tmp_path, "v1.2.3", output)

    flattened = [" ".join(command) for command in commands]
    assert not any(
        token in command
        for command in flattened
        for token in (" push ", " release create ", " release delete ", "--force")
    )


def test_release_publication_pushes_only_create_only_tag_after_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    asset = put(output, "asset.zip", b"fixture")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    checksums = put(output, "SHA256SUMS", f"{digest}  {asset.name}\n")
    provenance = put(
        output,
        "PROVENANCE.json",
        json.dumps(
            {
                "schema_version": 1,
                "version": "v9.9.9",
                "source_revision": "b" * 40,
                "artifacts": [
                    {"name": asset.name, "sha256": digest, "size": asset.stat().st_size}
                ],
            }
        )
        + "\n",
    )
    checksums.write_text(
        f"{digest}  {asset.name}\n"
        f"{hashlib.sha256(provenance.read_bytes()).hexdigest()}  PROVENANCE.json\n",
        encoding="ascii",
    )
    preflight = lifecycle.ReleaseBundle(
        version="v9.9.9",
        revision="b" * 40,
        output_dir=output,
        archives=[asset],
        checksums=checksums,
        provenance=provenance,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(lifecycle, "preflight_release", lambda *_a, **_k: preflight)
    monkeypatch.setattr(lifecycle, "verify_release_bundle", lambda *_a, **_k: preflight)

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if (
            argv[:2] == ["git", "rev-parse"]
            or argv[:2] == ["git", "ls-remote"]
            or argv[:3] == ["gh", "release", "view"]
        ):
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)

    publish_release(tmp_path, "v9.9.9", output)

    rendered = [" ".join(command) for command in commands]
    assert any(command.startswith("git tag v9.9.9 ") for command in rendered)
    assert any(command == "git push origin refs/tags/v9.9.9" for command in rendered)
    assert not any(command.startswith("gh release ") for command in rendered)
    assert not any("--force" in command or "delete" in command for command in rendered)


def dependency_fixture(root: Path, dynamic: bool = True) -> None:
    continue_version = "dynamic" if dynamic else "1.2.3"
    digests = {
        name: hashlib.sha256(name.encode("ascii")).hexdigest()
        for name in ("llama-swap", "continue-vsix", "kilo-vsix")
    }
    put(
        root,
        "VERSIONS.lock",
        "LLAMA_SWAP_VERSION=v236\n"
        f"LLAMA_SWAP_SHA256={digests['llama-swap']}\n"
        f"CONTINUE_VSIX_VERSION={continue_version}\n"
        "CONTINUE_VSIX_RESOLVED_VERSION=1.2.3\n"
        f"CONTINUE_VSIX_SHA256={digests['continue-vsix']}\n"
        "KILO_VSIX_VERSION=7.4.5\n"
        f"KILO_VSIX_SHA256={digests['kilo-vsix']}\n",
    )
    put(
        root,
        "serving/models.manifest",
        "# name | repo | include | slot | ctx | flags | revision\n"
        "chat | example/chat | model.gguf | fast | 32768 | --temp 0.7 | dynamic\n",
    )
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "llama-swap",
                        "kind": "native",
                        "version_key": "LLAMA_SWAP_VERSION",
                        "allow_dynamic": False,
                        "source": {
                            "identity": "https://example.invalid/llama-swap",
                            "digest_key": "LLAMA_SWAP_SHA256",
                        },
                    },
                    {
                        "id": "continue-vsix",
                        "kind": "ide-extension",
                        "version_key": "CONTINUE_VSIX_VERSION",
                        "allow_dynamic": True,
                        "source": {
                            "identity": "https://example.invalid/continue-vsix",
                            "resolved_version_key": "CONTINUE_VSIX_RESOLVED_VERSION",
                            "digest_key": "CONTINUE_VSIX_SHA256",
                        },
                    },
                    {
                        "id": "kilo-vsix",
                        "kind": "ide-extension",
                        "version_key": "KILO_VSIX_VERSION",
                        "allow_dynamic": False,
                        "source": {
                            "identity": "https://example.invalid/kilo-vsix",
                            "digest_key": "KILO_VSIX_SHA256",
                        },
                    },
                ]
            },
            indent=2,
        )
        + "\n",
    )


def test_exact_pin_validation_rejects_ranges_latest_and_unresolved_reproducible_inputs(
    tmp_path: Path,
) -> None:
    dependency_fixture(tmp_path)
    assert validate_dependency_inputs(tmp_path, reproducible=False) == []

    errors = validate_dependency_inputs(tmp_path, reproducible=True)
    assert any(
        "continue-vsix" in error and "resolved artifact" in error for error in errors
    )
    assert any(
        "model:chat" in error and "resolved artifact" in error for error in errors
    )

    versions = tmp_path / "VERSIONS.lock"
    versions.write_text(
        versions.read_text(encoding="utf-8").replace(
            "KILO_VSIX_VERSION=7.4.5", "KILO_VSIX_VERSION=latest"
        ),
        encoding="utf-8",
    )
    errors = validate_dependency_inputs(tmp_path, reproducible=False)
    assert any("KILO_VSIX_VERSION" in error and "exact" in error for error in errors)

    versions.write_text(
        versions.read_text(encoding="utf-8").replace(
            "KILO_VSIX_VERSION=latest", "KILO_VSIX_VERSION=>=7,<8"
        ),
        encoding="utf-8",
    )
    errors = validate_dependency_inputs(tmp_path, reproducible=False)
    assert any("KILO_VSIX_VERSION" in error and "exact" in error for error in errors)


def test_cached_artifact_manifest_records_and_verifies_hashes(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "dependency cache with spaces"
    source = put(tmp_path, "downloads/native artifact.zip", b"verified bytes")
    record = record_cached_artifact(
        cache,
        artifact_id="continue-vsix",
        source_file=source,
        source_url="https://example.invalid/continue.vsix",
        requested_version="dynamic",
        resolved_version="1.2.3",
    )

    assert record.sha256 == hashlib.sha256(b"verified bytes").hexdigest()
    manifest = cache / "manifest.json"
    assert validate_artifact_manifest(manifest, cache) == []

    cached = cache / record.cache_path
    cached.write_bytes(b"tampered")
    errors = validate_artifact_manifest(manifest, cache)
    assert any("checksum mismatch" in error for error in errors)


def test_cached_artifact_resolution_requires_matching_version_and_hash(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    source = put(tmp_path, "source/tool.zip", b"tool")
    record_cached_artifact(
        cache,
        artifact_id="tool",
        source_file=source,
        source_url=source.as_uri(),
        requested_version="v1.0.0",
        resolved_version="v1.0.0",
    )

    resolved = resolve_cached_artifact(
        cache / "manifest.json", cache, "tool", expected_version="v1.0.0"
    )
    assert resolved.read_bytes() == b"tool"
    with pytest.raises(LifecycleError, match="version"):
        resolve_cached_artifact(
            cache / "manifest.json", cache, "tool", expected_version="v2.0.0"
        )
    with pytest.raises(LifecycleError, match="requested"):
        resolve_cached_artifact(
            cache / "manifest.json",
            cache,
            "tool",
            expected_requested_version="dynamic",
        )


def test_policy_bound_artifact_resolution_is_scoped_to_requested_input(
    tmp_path: Path,
) -> None:
    dependency_fixture(tmp_path)
    cache = tmp_path / "cache"
    source = put(tmp_path, "download/llama-swap", b"llama-swap")
    record_cached_artifact(
        cache,
        artifact_id="llama-swap",
        source_file=source,
        source_url="https://example.invalid/llama-swap",
        requested_version="v236",
        resolved_version="v236",
        trust="policy-bound",
    )

    resolved = resolve_cached_artifact(
        cache / "manifest.json",
        cache,
        "llama-swap",
        expected_version="v236",
        expected_requested_version="v236",
        policy_root=tmp_path,
        require_policy_bound=True,
    )

    assert resolved.read_bytes() == b"llama-swap"


def test_dependency_export_fetches_local_fixture_and_rejects_wrong_expected_hash(
    tmp_path: Path,
) -> None:
    source = put(tmp_path, "source files/native tool.zip", b"native bytes")
    cache = tmp_path / "cache with spaces"
    expected = hashlib.sha256(source.read_bytes()).hexdigest()

    record = export_artifact(
        cache,
        artifact_id="native-tool",
        source_url=source.as_uri(),
        requested_version="v1.0.0",
        resolved_version="v1.0.0",
        expected_sha256=expected,
    )

    assert record.sha256 == expected
    assert validate_artifact_manifest(cache / "manifest.json", cache) == []

    untouched = tmp_path / "rejected cache"
    with pytest.raises(LifecycleError, match="expected SHA-256"):
        export_artifact(
            untouched,
            artifact_id="native-tool",
            source_url=source.as_uri(),
            requested_version="v1.0.0",
            resolved_version="v1.0.0",
            expected_sha256="0" * 64,
        )
    assert not (untouched / "manifest.json").exists()


def test_dependency_export_never_uses_source_basename_as_cache_scratch(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    first = put(tmp_path, "first/tool.zip", b"first")
    record_cached_artifact(
        cache,
        artifact_id="first",
        source_file=first,
        source_url=first.as_uri(),
        requested_version="1.0.0",
        resolved_version="1.0.0",
    )
    source_named_manifest = put(tmp_path, "second/manifest.json", b"second")

    export_artifact(
        cache,
        artifact_id="second",
        source_url=source_named_manifest.as_uri(),
        requested_version="2.0.0",
        resolved_version="2.0.0",
    )

    payload = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["id"] for entry in payload["artifacts"]} == {"first", "second"}


def test_model_snapshot_records_resolved_revision_and_file_hashes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cache = tmp_path / "dependency cache"
    root.mkdir()
    put(
        root,
        "serving/models.manifest",
        "chat | example/chat | *.gguf | fast | 32768 | | dynamic\n",
    )
    shard = put(root, "models/chat/model-00001-of-00002.gguf", b"first")
    put(root, "models/chat/model-00002-of-00002.gguf", b"second")

    record = record_model_snapshot(
        root,
        cache,
        model_name="chat",
        repository="example/chat",
        requested_revision="dynamic",
        resolved_revision="f" * 40,
    )

    assert record.artifact_id == "model:chat"
    assert record.resolved_version == "f" * 40
    assert validate_model_snapshot(root, cache, record) == []
    shard.write_bytes(b"other")
    assert any(
        "checksum mismatch" in error
        for error in validate_model_snapshot(root, cache, record)
    )


def test_reproducible_inputs_accept_resolved_hashed_artifacts(
    tmp_path: Path,
) -> None:
    dependency_fixture(tmp_path)
    cache = tmp_path / "cache"
    for artifact_id, resolved in (
        ("llama-swap", "v236"),
        ("continue-vsix", "1.2.3"),
        ("kilo-vsix", "7.4.5"),
    ):
        source = put(
            tmp_path,
            f"source/{artifact_id.replace(':', '-')}.bin",
            artifact_id.encode("ascii"),
        )
        record_cached_artifact(
            cache,
            artifact_id=artifact_id,
            source_file=source,
            source_url=f"https://example.invalid/{artifact_id}",
            requested_version="dynamic"
            if artifact_id in {"continue-vsix", "model:chat"}
            else resolved,
            resolved_version=resolved,
            trust="policy-bound",
        )
    models_manifest = tmp_path / "serving" / "models.manifest"
    models_manifest.write_text(
        models_manifest.read_text(encoding="utf-8").replace(
            "| dynamic\n", f"| {'f' * 40}\n"
        ),
        encoding="utf-8",
    )
    put(tmp_path, "models/chat/model.gguf", b"GGUF model")
    promote_fixture_models(
        tmp_path,
        cache,
        [
            (
                "chat",
                "example/chat",
                "model.gguf",
                "f" * 40,
                "model.gguf",
                b"GGUF model",
            )
        ],
    )

    assert (
        validate_dependency_inputs(
            tmp_path,
            artifact_manifest=cache / "manifest.json",
            cache_root=cache,
            reproducible=True,
        )
        == []
    )


def test_install_state_hashes_inputs_and_invalidates_only_stale_phases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install root with spaces"
    home = tmp_path / "home with spaces"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")

    first = initialize_install_state(root, home, source_revision="a" * 40)
    mark_install_phase(root, "bootstrap")
    assert phase_is_current(root, "bootstrap")

    owned = put(home, ".continue/config.oracle.yaml", "owned\n")
    register_owned_path(root, home, owned)
    second = initialize_install_state(root, home, source_revision="a" * 40)
    assert second["input_sha256"] == first["input_sha256"]
    assert phase_is_current(root, "bootstrap")
    assert len(second["owned_paths"]) == 1
    owned.write_text("later phase changed this path\n", encoding="utf-8")
    assert phase_is_current(root, "bootstrap")

    put(
        root,
        "incoming/dependency-cache/manifest.json",
        '{"schema_version": 1, "artifacts": []}\n',
    )
    exported = initialize_install_state(root, home, source_revision="a" * 40)
    assert exported["input_sha256"] != first["input_sha256"]
    assert not phase_is_current(root, "bootstrap")
    mark_install_phase(root, "bootstrap")

    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.1\n")
    upgraded = initialize_install_state(root, home, source_revision="b" * 40)
    assert upgraded["input_sha256"] != first["input_sha256"]
    assert not phase_is_current(root, "bootstrap")
    assert len(upgraded["owned_paths"]) == 1


def test_install_phase_validates_only_paths_owned_by_that_phase(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    initialize_install_state(root, home, source_revision="a" * 40)
    begin_install_phase(root, "bootstrap")
    tool = put(root, ".tools/bin/tool", "v1\n")
    register_owned_path(root, home, tool)
    mark_install_phase(root, "bootstrap")

    generated = put(root, "state/generated/config.json", "{}\n")
    register_owned_path(root, home, generated)
    generated.write_text('{"later": true}\n', encoding="utf-8")
    assert phase_is_current(root, "bootstrap")

    tool.write_text("v2\n", encoding="utf-8")
    assert not phase_is_current(root, "bootstrap")


def test_install_state_safely_migrates_legacy_phase_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    put(root, ".install-state", "done=bootstrap\n")

    state = initialize_install_state(root, home, source_revision="a" * 40)

    assert (root / ".install-state").is_dir()
    assert (root / ".install-state/state.json").is_file()
    assert state["phases"] == {}
    assert not phase_is_current(root, "bootstrap")


def test_install_state_uses_packaged_source_provenance_without_git(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extracted release"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    revision = "b" * 40
    put(
        root,
        "SOURCE-PROVENANCE.json",
        json.dumps({"schema_version": 1, "source_revision": revision}) + "\n",
    )

    state = initialize_install_state(root, home)

    assert state["source_revision"] == revision


def test_install_state_rejects_paths_outside_declared_roots(tmp_path: Path) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    outside = put(tmp_path, "unrelated/victim.txt", "keep\n")
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    initialize_install_state(root, home, source_revision="a" * 40)

    with pytest.raises(LifecycleError, match="outside"):
        register_owned_path(root, home, outside)


def test_uninstall_defaults_to_dry_run_and_preserves_unowned_user_configs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    initialize_install_state(root, home, source_revision="a" * 40)
    owned = put(home, ".continue/config.oracle.yaml", "owned\n")
    unrelated_continue = put(home, ".continue/config.yaml", "user\n")
    unrelated_kilo = put(home, ".config/kilo/user.json", '{"user": true}\n')
    register_owned_path(root, home, owned)

    plan = uninstall(root, home)

    assert plan.applied is False
    assert owned.exists()
    assert unrelated_continue.read_text(encoding="utf-8") == "user\n"
    assert unrelated_kilo.read_text(encoding="utf-8") == '{"user": true}\n'
    assert [entry.action for entry in plan.entries] == ["remove"]


def test_uninstall_removes_unchanged_owned_files_but_preserves_modified_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    initialize_install_state(root, home, source_revision="a" * 40)
    unchanged = put(home, ".continue/config.oracle.yaml", "owned\n")
    changed = put(home, ".config/kilo/kilo.oracle.jsonc", '{"owned": true}\n')
    register_owned_path(root, home, unchanged)
    register_owned_path(root, home, changed)
    changed.write_text('{"owned": true, "user_edit": true}\n', encoding="utf-8")

    plan = uninstall(root, home, apply=True)

    assert plan.applied is True
    assert not unchanged.exists()
    assert changed.exists()
    assert any(
        entry.path.endswith("kilo.oracle.jsonc") and entry.action == "preserve-modified"
        for entry in plan.entries
    )


def test_uninstall_removes_owned_tree_children_before_parents(tmp_path: Path) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    tree = root / ".tools"
    put(tree, "bin/tool", "owned\n")
    initialize_install_state(root, home, source_revision="a" * 40)
    lifecycle.register_owned_tree(root, home, tree)

    uninstall(root, home, apply=True)

    assert not tree.exists()


def test_uninstall_rejects_malicious_state_paths_without_touching_victim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    initialize_install_state(root, home, source_revision="a" * 40)
    victim = put(tmp_path, "victim.txt", "keep\n")
    state_path = root / ".install-state/state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["owned_paths"] = [
        {
            "scope": "install",
            "path": "../victim.txt",
            "kind": "file",
            "sha256": hashlib.sha256(b"keep\n").hexdigest(),
        }
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(LifecycleError, match="unsafe"):
        uninstall(root, home, apply=True)

    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_purge_requires_confirmation_and_never_deletes_user_config_trees(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "chat | repo | file | fast | 1 | | abc\n")
    initialize_install_state(root, home, source_revision="a" * 40)
    put(root, "models/model.gguf", b"GGUF")
    user_continue = put(home, ".continue/user.yaml", "keep\n")
    user_kilo = put(home, ".config/kilo/user.json", "keep\n")

    with pytest.raises(LifecycleError, match="confirmation"):
        uninstall(root, home, apply=True, purge=True)

    plan = uninstall(root, home, apply=True, purge=True, confirm_purge=True)

    assert not (root / "models").exists()
    assert not (root / ".install-state").exists()
    assert user_continue.exists()
    assert user_kilo.exists()
    assert any(entry.action == "purge" for entry in plan.entries)


def test_atomic_text_write_is_utf8_no_bom_and_preserves_old_file_on_replace_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = put(tmp_path, "path with spaces/config.json", '{"old": true}\n')
    atomic_write_text(target, '{"value": "caf\u00e9"}\n')
    assert target.read_bytes() == '{"value": "caf\u00e9"}\n'.encode("utf-8")
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")

    original = target.read_bytes()

    def fail_replace(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError("fixture replacement failure")

    monkeypatch.setattr(lifecycle.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement"):
        atomic_write_text(target, '{"new": true}\n')

    assert target.read_bytes() == original
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_atomic_write_replaces_final_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    victim = put(tmp_path, "outside/user.txt", "user data\n")
    target = tmp_path / "generated/config.txt"
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    atomic_write_text(target, "oracle data\n")

    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "oracle data\n"
    assert victim.read_text(encoding="utf-8") == "user data\n"


def test_atomic_write_does_not_resolve_the_final_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "generated/config.txt"
    original_resolve = Path.resolve

    def guarded_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if self == target:
            raise AssertionError("atomic write followed the final target")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    atomic_write_text(target, "safe\n")

    assert target.read_text(encoding="utf-8") == "safe\n"


def test_generated_model_configs_are_atomic_owned_and_preserve_user_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository with spaces"
    home = tmp_path / "home with spaces"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "# fixture\n")
    put(
        root,
        "serving/models.manifest",
        "# name | repo | include | slot | ctx | flags | revision\n"
        "chat-model | example/chat | model.gguf | fast | 32768 | --temp 0.7 | "
        + ("a" * 40)
        + "\n"
        "embed-model | example/embed | embed.gguf | embed | 8192 | | "
        + ("b" * 40)
        + "\n"
        "extra-model | example/extra | extra.gguf | big | 65536 | | "
        + ("d" * 40)
        + "\n",
    )
    put(root, "models/chat-model/model.gguf", b"GGUF chat")
    put(root, "models/embed-model/embed.gguf", b"GGUF embed")
    put(root, "models/extra-model/extra.gguf", b"GGUF extra")
    promote_fixture_models(
        root,
        root / "incoming/dependency-cache",
        [
            (
                "chat-model",
                "example/chat",
                "model.gguf",
                "a" * 40,
                "model.gguf",
                b"GGUF chat",
            ),
            (
                "embed-model",
                "example/embed",
                "embed.gguf",
                "b" * 40,
                "embed.gguf",
                b"GGUF embed",
            ),
            (
                "extra-model",
                "example/extra",
                "extra.gguf",
                "d" * 40,
                "extra.gguf",
                b"GGUF extra",
            ),
        ],
    )
    put(
        root,
        "serving/profiles.conf",
        "active | 8 | chat-model,embed-model | chat-model | chat-model | "
        "chat-model | fixture\n",
    )
    put(
        root,
        "state/generated/serving/admission.json",
        json.dumps(
            {
                "schema_version": 1,
                "tiers": {
                    "OPUS_MODEL": "chat-model",
                    "SONNET_MODEL": "chat-model",
                    "HAIKU_MODEL": "chat-model",
                },
                "models": {
                    "chat-model": {"advertised_context": 24576},
                    "embed-model": {"advertised_context": 4096},
                },
            }
        )
        + "\n",
    )
    claude_template = put(
        root,
        "engines/claude-code/home/settings.json",
        '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9099"}}\n',
    )
    opencode_template = put(
        root,
        "engines/opencode/xdg/opencode/opencode.json",
        '{"provider": {"oracle": {"models": {}}}}\n',
    )
    user_continue = put(home, ".continue/config.yaml", "name: User Config\n")
    user_kilo = put(home, ".config/kilo/kilo.jsonc", '{"user": true}\n')
    initialize_install_state(root, home, source_revision="c" * 40)
    template_hashes = {
        claude_template: hashlib.sha256(claude_template.read_bytes()).hexdigest(),
        opencode_template: hashlib.sha256(opencode_template.read_bytes()).hexdigest(),
    }

    written = sync_model_configs(root, home)
    first_bytes = {path: path.read_bytes() for path in written}
    written_again = sync_model_configs(root, home)

    assert written_again == written
    assert {path: path.read_bytes() for path in written_again} == first_bytes
    assert user_continue.read_text(encoding="utf-8") == "name: User Config\n"
    assert user_kilo.read_text(encoding="utf-8") == '{"user": true}\n'
    assert (root / "state/generated/continue/config.yaml").is_file()
    assert (root / "state/generated/kilo/kilo.jsonc").is_file()
    assert (root / "state/generated/claude-code/settings.json").is_file()
    assert (root / "state/generated/opencode/opencode.json").is_file()
    assert (root / "serving/tiers.env").read_text(encoding="utf-8") == (
        "OPUS_MODEL=chat-model\nSONNET_MODEL=chat-model\nHAIKU_MODEL=chat-model\n"
    )
    generated_opencode = json.loads(
        (root / "state/generated/opencode/opencode.json").read_text(encoding="utf-8")
    )
    assert set(generated_opencode["provider"]["oracle"]["models"]) == {"chat-model"}
    kilo_lines = (
        (root / "state/generated/kilo/kilo.jsonc")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    generated_kilo = json.loads("\n".join(kilo_lines[1:]))
    assert generated_kilo["mcp"]["lean-ctx"]["command"] == [
        "python",
        "{env:ORACLE_ROOT}/connectors/lean_ctx_mcp.py",
    ]
    assert (
        generated_kilo["mcp"]["lean-ctx"]["environment"]["LEAN_CTX_TOOL_PROFILE"]
        == "minimal"
    )
    assert (
        generated_kilo["mcp"]["lean-ctx"]["environment"]["LEAN_CTX_DISABLED_TOOLS"]
        == "ctx_call"
    )
    assert (
        generated_kilo["mcp"]["lean-ctx"]["environment"]["LEAN_CTX_PROJECT_ROOT"]
        == "{env:ORACLE_PROJECT_ROOT}"
    )
    for path, digest in template_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    for path in written:
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    state = json.loads((root / ".install-state/state.json").read_text(encoding="utf-8"))
    owned = {(item["scope"], item["path"]) for item in state["owned_paths"]}
    assert ("install", "state/generated/continue/config.yaml") in owned
    assert ("install", "state/generated/kilo/kilo.jsonc") in owned
    assert len(owned) == len(state["owned_paths"])


def test_generated_configs_refuse_symbolic_link_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "# fixture\n")
    put(
        root,
        "serving/models.manifest",
        "chat | example/chat | model.gguf | fast | 32768 | | " + "a" * 40 + "\n",
    )
    put(root, "models/chat/model.gguf", b"GGUF")
    promote_fixture_models(
        root,
        root / "incoming/dependency-cache",
        [
            (
                "chat",
                "example/chat",
                "model.gguf",
                "a" * 40,
                "model.gguf",
                b"GGUF",
            )
        ],
    )
    original_is_reparse = lifecycle._is_reparse_point
    linked_parent = root / "state"

    def fake_is_reparse(path: Path) -> bool:
        return path == linked_parent or original_is_reparse(path)

    monkeypatch.setattr(lifecycle, "_is_reparse_point", fake_is_reparse)
    with pytest.raises(LifecycleError, match="symbolic-link parent"):
        sync_model_configs(root, home)
    assert not (root / "state/generated/continue/config.yaml").exists()


def test_lifecycle_scripts_have_cross_platform_twins_and_protected_builder_is_unchanged() -> (
    None
):
    for name in ("package", "release", "uninstall", "export-dependencies"):
        assert (REPO_ROOT / "bootstrap" / f"{name}.ps1").is_file()
        assert (REPO_ROOT / "bootstrap" / f"{name}.sh").is_file()

    protected = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "diff",
            "4446a15..HEAD",
            "--",
            "bootstrap/build-installers.ps1",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert protected.stdout == ""


def test_install_consumers_use_export_cache_instead_of_dynamic_network_resolution() -> (
    None
):
    windows_serving = (REPO_ROOT / "serving/serve-windows.ps1").read_text(
        encoding="utf-8"
    )
    mac_install = (REPO_ROOT / "bootstrap/install.sh").read_text(encoding="utf-8")
    ide_powershell = (REPO_ROOT / "connectors/ide/setup-ide.ps1").read_text(
        encoding="utf-8"
    )
    ide_bash = (REPO_ROOT / "connectors/ide/setup-ide.sh").read_text(encoding="utf-8")
    windows_cli = (REPO_ROOT / "bin/oracle.ps1").read_text(encoding="utf-8")

    assert "Invoke-WebRequest" not in windows_serving
    assert "releases/download" not in windows_serving
    assert "releases/download" not in mac_install
    assert "/latest" not in ide_powershell
    assert "/latest" not in ide_bash
    assert (REPO_ROOT / "env/uv.lock").is_file()
    assert "uv sync --offline --frozen" in mac_install
    for source in (
        windows_serving,
        mac_install,
        ide_powershell,
        ide_bash,
        windows_cli,
    ):
        assert "dependency-cache" in source
        assert "artifact-path" in source
    policy = json.loads(
        (REPO_ROOT / "verification/policy.json").read_text(encoding="utf-8")
    )
    declared = {item["id"] for item in policy["dependency_inputs"]}
    consumed = {
        "llama-swap-darwin-arm64",
        "llama-swap-windows-amd64",
        "llama-cpp-windows-vulkan",
        "brew-llama-cpp",
        "node-darwin-arm64",
        "node-windows-x64",
        "uv-darwin-arm64",
        "uv-windows-x64",
        "jq-darwin-arm64",
        "lean-ctx-darwin-arm64",
        "lean-ctx-windows-x64",
        "vscodium-darwin-arm64",
        "vscodium-darwin-x64",
        "vscodium-windows-x64",
        "npm-claude-code",
        "npm-opencode",
        "npm-kilo-cli",
        "continue-vsix-darwin-arm64",
        "continue-vsix-darwin-x64",
        "continue-vsix-windows-x64",
        "kilo-vsix-darwin-arm64",
        "kilo-vsix-darwin-x64",
        "kilo-vsix-windows-x64",
    }
    combined = "\n".join(
        (windows_serving, mac_install, ide_powershell, ide_bash, windows_cli)
    )
    assert consumed <= declared
    for artifact_id in consumed:
        assert artifact_id in combined


def test_lean_ctx_dependency_is_exact_policy_bound_and_installed_offline() -> None:
    versions, errors = lifecycle._parse_versions_text(
        (REPO_ROOT / "VERSIONS.lock").read_text(encoding="utf-8")
    )
    assert errors == []
    assert versions["LEAN_CTX_VERSION"] == "v3.9.3"
    assert versions["LEAN_CTX_COMMIT"] == "4e728c0e2c8cfc3516d4f38241d0dee37e0cb792"
    assert (
        versions["LEAN_CTX_DARWIN_ARM64_SHA256"]
        == "2adf67848020a198fd08eeeafc8957b83cf793b42719a196ea4b3a40a2597eb8"
    )
    assert (
        versions["LEAN_CTX_WINDOWS_X64_SHA256"]
        == "d4f0037df84e60a9761c0717b0441179d09560f8cc89e511340d31b75d89775f"
    )

    policy = json.loads(
        (REPO_ROOT / "verification/policy.json").read_text(encoding="utf-8")
    )
    inputs = {item["id"]: item for item in policy["dependency_inputs"]}
    expected = {
        "lean-ctx-darwin-arm64": (
            "https://github.com/yvgude/lean-ctx/releases/download/"
            "{version}/lean-ctx-aarch64-apple-darwin.tar.gz",
            "LEAN_CTX_DARWIN_ARM64_SHA256",
        ),
        "lean-ctx-windows-x64": (
            "https://github.com/yvgude/lean-ctx/releases/download/"
            "{version}/lean-ctx-x86_64-pc-windows-msvc.zip",
            "LEAN_CTX_WINDOWS_X64_SHA256",
        ),
    }
    for artifact_id, (identity, digest_key) in expected.items():
        declaration = inputs[artifact_id]
        assert declaration["kind"] == "native"
        assert declaration["version_key"] == "LEAN_CTX_VERSION"
        assert declaration["allow_dynamic"] is False
        assert declaration["source"] == {
            "identity": identity,
            "digest_key": digest_key,
        }

    mac = (REPO_ROOT / "bootstrap/install.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "bin/oracle.ps1").read_text(encoding="utf-8")
    assert 'install_cached_binary "lean-ctx-darwin-arm64"' in mac
    assert '"$ROOT/.tools/bin/lean-ctx"' in mac
    assert 'Get-CachedArtifact "lean-ctx-windows-x64"' in windows
    assert "engines/shared/lean-ctx-config.toml" in mac
    assert "engines\\shared\\lean-ctx-config.toml" in windows
    assert "state/lean-ctx/config/config.toml" in mac
    assert "state\\lean-ctx" in windows
    assert "config\\config.toml" in windows
    assert "lean-ctx/releases" not in mac
    assert "lean-ctx/releases" not in windows
    assert "Invoke-WebRequest" not in windows


def test_lean_ctx_mcp_only_wiring_is_repo_local_and_air_gapped() -> None:
    expected_environment = {
        "LEAN_CTX_CONFIG_DIR": "${ORACLE_ROOT}/state/lean-ctx/config",
        "LEAN_CTX_DATA_DIR": "${ORACLE_ROOT}/state/lean-ctx/data",
        "LEAN_CTX_STATE_DIR": "${ORACLE_ROOT}/state/lean-ctx/state",
        "LEAN_CTX_CACHE_DIR": "${ORACLE_ROOT}/state/lean-ctx/cache",
        "LEAN_CTX_PROJECT_ROOT": "${ORACLE_PROJECT_ROOT}",
        "LEAN_CTX_TOOL_PROFILE": "minimal",
        "LEAN_CTX_DISABLED_TOOLS": "ctx_call",
        "LEAN_CTX_NO_UPDATE_CHECK": "1",
        "LEAN_CTX_AUTONOMY": "false",
        "LEAN_CTX_NO_HOOK": "1",
        "LEAN_CTX_RULES_INJECTION": "off",
    }
    claude = json.loads(
        (REPO_ROOT / "connectors/mcp.claude.json").read_text(encoding="utf-8")
    )
    assert claude["mcpServers"]["lean-ctx"] == {
        "command": "python",
        "args": ["${ORACLE_ROOT}/connectors/lean_ctx_mcp.py"],
        "env": expected_environment,
    }
    opencode = json.loads(
        (
            REPO_ROOT / "engines/opencode/xdg/opencode/opencode.json"
        ).read_text(encoding="utf-8")
    )
    expected_opencode = {
        key: value.replace("${ORACLE_ROOT}", "{env:ORACLE_ROOT}").replace(
            "${ORACLE_PROJECT_ROOT}", "{env:ORACLE_PROJECT_ROOT}"
        )
        for key, value in expected_environment.items()
    }
    assert opencode["mcp"]["lean-ctx"] == {
        "type": "local",
        "command": [
            "python",
            "{env:ORACLE_ROOT}/connectors/lean_ctx_mcp.py",
        ],
        "environment": expected_opencode,
        "enabled": True,
    }
    cursor = json.loads(
        (REPO_ROOT / ".cursor/mcp.json").read_text(encoding="utf-8")
    )
    assert cursor["mcpServers"]["lean-ctx"]["type"] == "stdio"
    assert cursor["mcpServers"]["lean-ctx"]["command"] == "python"
    assert cursor["mcpServers"]["lean-ctx"]["args"] == [
        "${workspaceFolder}/connectors/lean_ctx_mcp.py"
    ]
    assert cursor["mcpServers"]["lean-ctx"]["env"]["LEAN_CTX_TOOL_PROFILE"] == "minimal"
    runtime_policy = tomllib.loads(
        (
            REPO_ROOT / "engines/shared/lean-ctx-config.toml"
        ).read_text(encoding="utf-8")
    )
    assert runtime_policy["tool_profile"] == "minimal"
    assert runtime_policy["disabled_tools"] == ["ctx_call"]
    assert runtime_policy["rules_injection"] == "off"
    assert runtime_policy["shell_hook_disabled"] is True
    assert runtime_policy["shell_security"] == "enforce"
    assert runtime_policy["shell_allow_writes"] is False
    assert runtime_policy["team_auto_push"] is False
    assert runtime_policy["cloud"] == {
        "auto_sync": False,
        "contribute_enabled": False,
    }
    assert runtime_policy["setup"] == {
        "auto_inject_rules": False,
        "auto_inject_skills": False,
        "auto_update_mcp": False,
    }
    assert set(runtime_policy["shell_allowlist"]) == {"pwd", "ls", "dir"}

    for relative in (
        "engines/shared/lean-ctx-env.sh",
        "engines/shared/lean-ctx-env.ps1",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "LEAN_CTX_NO_UPDATE_CHECK" in source
        assert "LEAN_CTX_AUTONOMY" in source
        assert "LEAN_CTX_NO_HOOK" in source
        assert "LEAN_CTX_RULES_INJECTION" in source
        assert "LEAN_CTX_TOOL_PROFILE" in source
        assert "LEAN_CTX_DISABLED_TOOLS" in source
        assert "ctx_call" in source
        assert "state" in source and "lean-ctx" in source
        assert "ORACLE_PROJECT_ROOT" in source
    for relative in (
        "engines/claude-code/launch.sh",
        "engines/claude-code/launch.ps1",
        "engines/opencode/launch.sh",
        "engines/opencode/launch.ps1",
        "engines/kilo/launch.sh",
        "engines/kilo/launch.ps1",
        "connectors/ide/setup-ide.sh",
        "connectors/ide/setup-ide.ps1",
    ):
        assert "lean-ctx-env" in (REPO_ROOT / relative).read_text(encoding="utf-8")

    rule = (REPO_ROOT / ".cursor/rules/lean-ctx.mdc").read_text(encoding="utf-8")
    assert "When the `ctx_*` tools are available" in rule
    assert "raw diagnostic" in rule
    assert "hook rewrite" not in rule
    assert not (REPO_ROOT / ".cursor/hooks.json").exists()
    for relative in ("bin/oracle", "bin/oracle.ps1"):
        operator = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "read-only local diagnostics only" in operator
        assert "doctor --fix" not in operator
        assert all(
            command in operator
            for command in ("status", "gain", "doctor", "benchmark")
        )
    mac_operator = (REPO_ROOT / "bin/oracle").read_text(encoding="utf-8")
    assert 'exec "$ROOT/.tools/bin/lean-ctx"' in mac_operator
    assert "exec lean-ctx" not in mac_operator
    for relative in ("connectors/ide/agent-tab.sh", "connectors/ide/agent-tab.ps1"):
        assert "ORACLE_PROJECT_ROOT" in (
            REPO_ROOT / relative
        ).read_text(encoding="utf-8")
    conductor = (REPO_ROOT / "conductor/conductor.py").read_text(encoding="utf-8")
    assert 'env["ORACLE_PROJECT_ROOT"] = str(cwd.resolve())' in conductor
    assert 'env["LEAN_CTX_PROJECT_ROOT"] = str(cwd.resolve())' in conductor


def test_lean_ctx_mcp_guard_filters_prompt_tools_and_hidden_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard_path = REPO_ROOT / "connectors/lean_ctx_mcp.py"
    spec = importlib.util.spec_from_file_location("oracle_lean_ctx_mcp", guard_path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    initialized = guard.sanitize_server_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "serverInfo": {"name": "lean-ctx", "version": "3.9.3"},
                "instructions": "Use ctx_call and unavailable tools.",
            },
        }
    )
    instructions = initialized["result"]["instructions"]
    assert "ctx_call" not in instructions
    assert "Native tools remain valid" in instructions
    advertised = guard.sanitize_server_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {"name": "ctx_read"},
                    {"name": "ctx_call"},
                    {"name": "ctx_shell"},
                ]
            },
        }
    )
    assert [tool["name"] for tool in advertised["result"]["tools"]] == [
        "ctx_read",
        "ctx_shell",
    ]
    runtime_policy = tomllib.loads(
        (
            REPO_ROOT / "engines/shared/lean-ctx-config.toml"
        ).read_text(encoding="utf-8")
    )
    assert set(runtime_policy["shell_allowlist"]) == guard.ALLOWED_SHELL_COMMANDS
    assert guard.blocked_tool_response(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ctx_call", "arguments": {}},
        }
    )["result"]["isError"] is True
    assert (
        guard.blocked_tool_response(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "ctx_read", "arguments": {}},
            }
        )
        is None
    )
    assert (
        guard.blocked_tool_response(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "ctx_shell",
                    "arguments": {"command": "pwd"},
                },
            }
        )
        is None
    )
    for request_id, command in enumerate(
        (
            "git status --short",
            "grep TODO README.md | wc -l",
            "uniq README.md AGENTS.md",
            "tree -o AGENTS.md",
            "where -InputObject x -FilterScript { Start-Process calc }",
            "cat /etc/passwd",
            r"type C:\private\secret.txt",
            r"type \\host\share\file",
            "ls ..",
            "rg TODO",
            r"""rg --pre\='sh -c "touch PWNED"' TODO README.md""",
            'rg --pre^="cmd /c echo PWNED" TODO README.md',
        ),
        start=6,
    ):
        blocked = guard.blocked_tool_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "ctx_shell", "arguments": {"command": command}},
            }
        )
        assert blocked["result"]["isError"] is True

    runtime_root = tmp_path / "runtime"
    binary_name = "lean-ctx.exe" if os.name == "nt" else "lean-ctx"
    binary = runtime_root / ".tools/bin" / binary_name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"policy-bound binary")
    template = runtime_root / "engines/shared/lean-ctx-config.toml"
    policy = runtime_root / "state/lean-ctx/config/config.toml"
    template.parent.mkdir(parents=True)
    policy.parent.mkdir(parents=True)
    template.write_text("tool_profile = \"minimal\"\n", encoding="utf-8")
    policy.write_bytes(template.read_bytes())
    git_dir = runtime_root / ".git/worktrees/feature"
    git_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    monkeypatch.delenv("ORACLE_ROOT", raising=False)
    monkeypatch.setenv("LEAN_CTX_PROJECT_ROOT", str(worktree))
    command, child_env = guard._lean_ctx_runtime()
    assert Path(command) == binary
    assert Path(child_env["ORACLE_ROOT"]) == runtime_root
    assert Path(child_env["LEAN_CTX_CONFIG_DIR"]) == policy.parent

    policy.write_text("tool_profile = \"power\"\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="policy is missing or differs"):
        guard._lean_ctx_runtime()

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    path_binary = tmp_path / "path-bin" / binary_name
    path_binary.parent.mkdir()
    path_binary.write_bytes(b"unmanaged binary")
    monkeypatch.setenv("LEAN_CTX_PROJECT_ROOT", str(isolated))
    monkeypatch.setenv("PATH", str(path_binary.parent))
    with pytest.raises(RuntimeError, match="policy-bound lean-ctx is missing"):
        guard._lean_ctx_runtime()


def test_doctors_report_dependency_cache_and_install_state_health() -> None:
    for relative in ("bootstrap/doctor.ps1", "bootstrap/doctor.sh"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "validate-dependencies" in source
        assert "dependency-cache" in source
        assert ".install-state" in source
        assert "lean-ctx" in source
        assert "LEAN_CTX_VERSION" in source
        assert "lean-ctx-config.toml" in source


def test_platform_generated_config_writers_use_same_directory_atomic_replacement() -> (
    None
):
    render = (REPO_ROOT / "bootstrap/render-config.sh").read_text(encoding="utf-8")
    service = (REPO_ROOT / "serving/service.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    shared = (REPO_ROOT / "verification/serving.py").read_text(encoding="utf-8")

    assert "serving/service.sh" in render
    assert 'mktemp "${PLIST}.tmp.' in service
    assert "verification\\serving.py" in windows
    assert "atomic_write_text(output" in shared
    assert "atomic_write_text(" in shared
    assert "metadata_path" in shared


def test_model_downloaders_enforce_promoted_revision_and_shard_hashes() -> None:
    bash_source = (REPO_ROOT / "bootstrap/download-models.sh").read_text(
        encoding="utf-8"
    )
    powershell_source = (REPO_ROOT / "bootstrap/download-models.ps1").read_text(
        encoding="utf-8"
    )

    assert '--revision "$revision"' in bash_source
    assert "model-authorities.json" in bash_source
    assert "import-model" in bash_source
    assert "record-model" not in bash_source
    assert "Revision = $f[6].Trim()" in powershell_source
    assert "model-authorities.json" in powershell_source
    assert "Get-FileHash" in powershell_source
    assert "import-model" in powershell_source
    assert "record-model" not in powershell_source
    assert "tree/main" not in powershell_source
    assert "resolve/main" not in powershell_source
    render_source = (REPO_ROOT / "verification/serving.py").read_text(encoding="utf-8")
    assert "def parse_manifest(" in render_source
    assert "validate_policy_bound_models" in render_source


def test_review_trust_rejects_self_asserted_hash_url_and_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cache = tmp_path / "cache"
    root.mkdir()
    trusted = put(tmp_path, "sources/trusted.bin", b"trusted bytes")
    attacker = put(tmp_path, "sources/attacker.bin", b"attacker bytes")
    trusted_digest = hashlib.sha256(trusted.read_bytes()).hexdigest()
    attacker_digest = hashlib.sha256(attacker.read_bytes()).hexdigest()
    put(
        root,
        "VERSIONS.lock",
        f"TOOL_VERSION=1.2.3\nTOOL_SHA256={trusted_digest}\n",
    )
    put(root, "serving/models.manifest", "# fixture\n")
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "trusted-tool",
                        "kind": "native",
                        "version_key": "TOOL_VERSION",
                        "allow_dynamic": False,
                        "source": {
                            "identity": trusted.as_uri(),
                            "digest_key": "TOOL_SHA256",
                        },
                    }
                ]
            }
        )
        + "\n",
    )
    export_artifact(
        cache,
        artifact_id="trusted-tool",
        source_url=attacker.as_uri(),
        requested_version="1.2.3",
        resolved_version="1.2.3",
        expected_sha256=attacker_digest,
    )

    errors = validate_dependency_inputs(
        root,
        artifact_manifest=cache / "manifest.json",
        cache_root=cache,
        reproducible=True,
    )

    assert any("authoritative source" in error for error in errors)
    assert any("trusted digest" in error for error in errors)


def test_review_trust_accepts_only_policy_bound_bytes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    cache = tmp_path / "cache"
    root.mkdir()
    trusted = put(tmp_path, "sources/trusted.bin", b"trusted bytes")
    trusted_digest = hashlib.sha256(trusted.read_bytes()).hexdigest()
    put(
        root,
        "VERSIONS.lock",
        f"TOOL_VERSION=1.2.3\nTOOL_SHA256={trusted_digest}\n",
    )
    put(root, "serving/models.manifest", "# fixture\n")
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "trusted-tool",
                        "kind": "native",
                        "version_key": "TOOL_VERSION",
                        "allow_dynamic": False,
                        "source": {
                            "identity": trusted.as_uri(),
                            "digest_key": "TOOL_SHA256",
                        },
                    }
                ]
            }
        )
        + "\n",
    )
    export_artifact(
        cache,
        artifact_id="trusted-tool",
        source_url=trusted.as_uri(),
        requested_version="1.2.3",
        resolved_version="1.2.3",
        policy_root=root,
        trusted=True,
    )

    assert (
        validate_dependency_inputs(
            root,
            artifact_manifest=cache / "manifest.json",
            cache_root=cache,
            reproducible=True,
        )
        == []
    )


def test_review_kind_aware_trust_rejects_tags_and_unresolved_digests(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    put(
        root,
        "VERSIONS.lock",
        "SOURCE_VERSION=v1.0.0\n"
        "SOURCE_REPO=https://example.invalid/source.git\n"
        "SOURCE_COMMIT=unresolved\n"
        "IMAGE_VERSION=example/image:1.0\n"
        "IMAGE_DIGEST=unresolved\n",
    )
    put(root, "serving/models.manifest", "# fixture\n")
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "source",
                        "kind": "git",
                        "version_key": "SOURCE_VERSION",
                        "allow_dynamic": False,
                        "source": {
                            "identity_key": "SOURCE_REPO",
                            "revision_key": "SOURCE_COMMIT",
                        },
                    },
                    {
                        "id": "image",
                        "kind": "container",
                        "version_key": "IMAGE_VERSION",
                        "allow_dynamic": False,
                        "source": {
                            "identity": "oci://example/image",
                            "digest_key": "IMAGE_DIGEST",
                        },
                    },
                ]
            }
        )
        + "\n",
    )

    errors = validate_dependency_inputs(root, reproducible=True)

    assert any("immutable trusted revision" in error for error in errors)
    assert any("immutable trusted digest" in error for error in errors)


def test_review_reproducible_models_reject_dynamic_revision_even_if_self_recorded(
    tmp_path: Path,
) -> None:
    dependency_fixture(tmp_path)
    cache = tmp_path / "cache"
    put(tmp_path, "models/chat/model.gguf", b"GGUF")
    record_model_snapshot(
        tmp_path,
        cache,
        model_name="chat",
        repository="example/chat",
        requested_revision="dynamic",
        resolved_revision="f" * 40,
    )

    errors = validate_dependency_inputs(
        tmp_path,
        artifact_manifest=cache / "manifest.json",
        cache_root=cache,
        reproducible=True,
    )

    assert any(
        "model:chat" in error and "trusted revision" in error for error in errors
    )


def test_review_offline_install_has_no_implicit_network_resolution() -> None:
    install_sources = {
        relative: (REPO_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "install",
            "bin/oracle",
            "bin/oracle.ps1",
            "bootstrap/install.sh",
            "connectors/ide/setup-ide.sh",
            "connectors/ide/setup-ide.ps1",
            "harness/ecc/install-ecc.sh",
            "harness/skill-packs/install-skill-packs.sh",
        )
    }
    forbidden = (
        "brew install",
        "winget install",
        "git clone",
        "xcode-select --install",
        "uv run",
        'opencode" models',
    )
    for relative, source in install_sources.items():
        for needle in forbidden:
            assert needle not in source, f"{relative} still contains {needle!r}"
    assert "--reproducible" in install_sources["bootstrap/install.sh"]
    assert "bootstrap/download-models.sh" in install_sources["install"]
    assert "ORACLE_CONNECTED_SETUP" in install_sources["install"]
    assert "bootstrap/download-models.sh" not in install_sources["bootstrap/install.sh"]
    for relative in ("bootstrap/doctor.sh", "bootstrap/doctor.ps1"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "--reproducible" in source


def test_review_uninstall_refuses_symlink_ancestor_escape(tmp_path: Path) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    owned = put(root, "managed/deep/owned.txt", "owned")
    victim = put(outside, "deep/owned.txt", "real user data")
    initialize_install_state(root, home, source_revision="a" * 40)
    register_owned_path(root, home, owned)
    (root / "managed" / "deep" / "owned.txt").unlink()
    (root / "managed" / "deep").rmdir()
    (root / "managed").rmdir()
    try:
        (root / "managed").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create directory symlinks: {exc}")

    with pytest.raises(LifecycleError, match="symlink or reparse ancestor"):
        uninstall(root, home, apply=True)

    assert victim.read_text(encoding="utf-8") == "real user data"


def test_review_purge_refuses_junction_like_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    victim = put(tmp_path, "outside/skill-packs/vendor/user.txt", "real user data")
    initialize_install_state(root, home, source_revision="b" * 40)
    (root / "harness" / "skill-packs" / "vendor").mkdir(parents=True)
    real_check = lifecycle._is_reparse_point

    def junction_like(path: Path) -> bool:
        if path == root / "harness":
            return True
        return real_check(path)

    monkeypatch.setattr(lifecycle, "_is_reparse_point", junction_like)

    with pytest.raises(LifecycleError, match="symlink or reparse ancestor"):
        uninstall(root, home, apply=True, purge=True, confirm_purge=True)

    assert victim.read_text(encoding="utf-8") == "real user data"


def test_review_purge_unlinks_final_junction_without_tree_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    initialize_install_state(root, home, source_revision="2" * 40)
    junction = root / ".tools"
    junction.mkdir()
    real_check = lifecycle._is_reparse_point
    real_rmtree = lifecycle.shutil.rmtree

    monkeypatch.setattr(
        lifecycle,
        "_is_reparse_point",
        lambda path: path == junction or real_check(path),
    )

    def guarded_rmtree(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path) == junction:
            pytest.fail("purge traversed a final junction")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.shutil, "rmtree", guarded_rmtree)

    uninstall(root, home, apply=True, purge=True, confirm_purge=True)

    assert not junction.exists()


def test_review_purge_confirmation_uses_exact_true_or_one_semantics() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "$(filter 1 true,$(CONFIRM_PURGE))" in makefile
    assert "$(if $(CONFIRM_PURGE),--confirm-purge,)" not in makefile


def test_review_apply_uninstall_stops_owned_service_before_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    owned = put(root, "runtime/service.cfg", "owned")
    initialize_install_state(root, home, source_revision="c" * 40)
    register_owned_path(root, home, owned)
    lifecycle.register_owned_service(
        root, kind="launchd-user", identifier="com.sentivue.llamaswap"
    )
    observations: list[tuple[str, bool]] = []

    def stop(service: dict[str, object]) -> None:
        observations.append((str(service["identifier"]), owned.exists()))

    uninstall(root, home, apply=True, service_stopper=stop)

    assert observations == [("com.sentivue.llamaswap", True)]
    assert not owned.exists()
    state = json.loads((root / ".install-state/state.json").read_text(encoding="utf-8"))
    assert state["owned_services"] == []


def test_review_windows_service_ownership_is_narrowly_registered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    initialize_install_state(root, home, source_revision="c" * 40)

    lifecycle.register_owned_service(
        root,
        kind="windows-scheduled-task",
        identifier="SentiVueOracleServing",
    )
    with pytest.raises(LifecycleError, match="unsafe owned service"):
        lifecycle.register_owned_service(
            root,
            kind="windows-scheduled-task",
            identifier="UnrelatedTask",
        )

    state = json.loads((root / ".install-state/state.json").read_text(encoding="utf-8"))
    assert state["owned_services"] == [
        {
            "identifier": "SentiVueOracleServing",
            "kind": "windows-scheduled-task",
        }
    ]
    serving = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    assert "state own-service --root $Root" in serving
    assert '--service-kind "windows-scheduled-task"' in serving
    assert "--identifier $TaskName" in serving


def test_review_generated_engine_configs_are_selected_by_launchers() -> None:
    sources = {
        relative: (REPO_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "engines/claude-code/launch.sh",
            "engines/claude-code/launch.ps1",
            "engines/opencode/launch.sh",
            "engines/opencode/launch.ps1",
            "engines/kilo/launch.sh",
            "engines/kilo/launch.ps1",
        )
    }

    assert (
        "state/generated/claude-code/settings.json"
        in sources["engines/claude-code/launch.sh"]
    )
    assert "--settings" in sources["engines/claude-code/launch.sh"]
    assert (
        "state\\generated\\claude-code\\settings.json"
        in sources["engines/claude-code/launch.ps1"]
    )
    assert "--settings" in sources["engines/claude-code/launch.ps1"]
    assert "OPENCODE_CONFIG" in sources["engines/opencode/launch.sh"]
    assert (
        "state/generated/opencode/opencode.json"
        in sources["engines/opencode/launch.sh"]
    )
    assert "OPENCODE_CONFIG" in sources["engines/opencode/launch.ps1"]
    assert (
        "state\\generated\\opencode\\opencode.json"
        in sources["engines/opencode/launch.ps1"]
    )
    for relative in ("engines/kilo/launch.sh", "engines/kilo/launch.ps1"):
        assert "KILO_CONFIG" in sources[relative]
        assert "state/generated/kilo/kilo.jsonc" in sources[relative].replace("\\", "/")


def test_review_generated_ide_configs_are_explicitly_selected() -> None:
    shell = (REPO_ROOT / "connectors/ide/setup-ide.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "connectors/ide/setup-ide.ps1").read_text(
        encoding="utf-8"
    )

    for source in (shell, powershell):
        assert "CONTINUE_GLOBAL_DIR" in source
        assert "KILO_CONFIG" in source
        assert "state" in source
        assert "generated" in source
        assert "--user-data-dir" in source
        assert "--extensions-dir" in source
        assert ".tools" in source
        assert "vscodium" in source.lower()
        assert "continue.configPath" not in source
        assert "kilo-code.configPath" not in source
    assert "Application Support/VSCodium" not in shell
    assert "$env:APPDATA" not in powershell


def test_review_owned_tree_tracks_every_path_and_invalidates_changed_phase(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    tree = root / ".tools"
    first = put(tree, "bin/tool", "version one")
    second = put(tree, "share/data.txt", "data")
    initialize_install_state(root, home, source_revision="d" * 40)

    lifecycle.register_owned_tree(root, home, tree)
    mark_install_phase(root, "toolchain")

    state = json.loads((root / ".install-state/state.json").read_text(encoding="utf-8"))
    owned = {(item["kind"], item["path"]) for item in state["owned_paths"]}
    assert ("directory", ".tools") in owned
    assert ("directory", ".tools/bin") in owned
    assert ("file", ".tools/bin/tool") in owned
    assert ("file", ".tools/share/data.txt") in owned
    assert phase_is_current(root, "toolchain")

    first.write_text("version two", encoding="utf-8")
    assert not phase_is_current(root, "toolchain")
    assert second.read_text(encoding="utf-8") == "data"


def test_review_installers_check_native_failures_and_replace_stale_tools() -> None:
    windows = (REPO_ROOT / "bin/oracle.ps1").read_text(encoding="utf-8")
    serving = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    mac = (REPO_ROOT / "bootstrap/install.sh").read_text(encoding="utf-8")

    assert "function Invoke-NativeChecked" in windows
    assert 'Invoke-NativeChecked "npm"' in windows
    assert 'Invoke-NativeChecked "sync-skills"' in windows
    assert "state own-tree" in windows
    assert "if (-not (Test-Path $swapExe))" not in serving
    assert "if (-not (Test-Path $serverExe))" not in serving
    assert "state own-tree" in mac


def test_review_protected_installer_transform_is_atomic_utf8_and_fail_closed(
    tmp_path: Path,
) -> None:
    tar_payload, zip_payload, _files = one_click_payloads()
    mac = put(
        tmp_path,
        "SentiVue-Oracle-Installer-v1.2.3.command",
        b"#!/bin/bash\nbash install || true\n__PAYLOAD_BELOW__\n" + tar_payload,
    )
    windows_script = """@echo off
#==PSPAYLOAD==#
$ErrorActionPreference = "Stop"
if ($sel.Name -eq "full") { Remove-Item (Join-Path $dest "serving\\models.profile") -ErrorAction SilentlyContinue }
else { Set-Content -Path (Join-Path $dest "serving\\models.profile") -Value (($sel.Models -split ",") -join "`n") }
Set-Content -Path (Join-Path $dest "serving\\tiers.env") -Value @("OPUS_MODEL=$($sel.Opus)", "SONNET_MODEL=$($sel.Sonnet)", "HAIKU_MODEL=$($sel.Haiku)")
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "connectors\\ide\\setup-ide.ps1") install
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\\download-models.ps1")
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bin\\oracle.ps1") setup
#==B64PAYLOAD==#
__PAYLOAD__
""".replace("__PAYLOAD__", base64.b64encode(zip_payload).decode("ascii"))
    windows = put(
        tmp_path,
        "SentiVue-Oracle-Setup-v1.2.3.cmd",
        windows_script.encode("ascii"),
    )

    mac_record = lifecycle._harden_built_installer(mac)
    windows_record = lifecycle._harden_built_installer(windows)

    assert mac_record["id"] == lifecycle.INSTALLER_HARDENING_TRANSFORM
    assert b"bash install || true" not in mac.read_bytes()
    assert lifecycle.INSTALLER_HARDENING_TRANSFORM.encode("ascii") in mac.read_bytes()
    hardened = windows.read_text(encoding="ascii")
    assert "Set-Content -Path" not in hardened
    assert "Write-Utf8NoBomAtomic" in hardened
    assert "New-Object Text.UTF8Encoding($false)" in hardened
    assert "Move-Item -LiteralPath $temporary" in hardened
    assert "download-models.ps1" in hardened
    assert "model acquisition failed" in hardened
    assert "ORACLE_CONNECTED_SETUP" in hardened
    assert "embedded payload SHA-256 mismatch" in hardened
    assert "archive member has a traversal component" in hardened
    assert "existing unowned, different-version" in hardened
    assert "ORACLE_INSTALLER_SKIP_SETUP" in hardened
    assert hardened.count("if ($LASTEXITCODE -ne 0)") >= 5
    assert "ownership-scoped uninstaller failed" in hardened
    assert mac_record["payload_sha256"] == hashlib.sha256(tar_payload).hexdigest()
    assert windows_record["payload_sha256"] == hashlib.sha256(zip_payload).hexdigest()
    mac_header = mac.read_bytes().split(b"\n__PAYLOAD_BELOW__\n", 1)[0]
    assert b"tar -tzf" in mac_header
    assert b"tar -tvzf" in mac_header
    assert b"mktemp -d" in mac_header
    assert b"existing unowned, different-version" in mac_header
    assert b"Python 3.12 or newer is required for safe" not in mac_header
    assert b'mv -n "$source" "$destination"' in mac_header
    assert b"renamex_np" not in mac_header
    assert b"xcrun --find clang" not in mac_header
    assert b"rollback_new_install" in mac_header
    records_match = re.search(rb'SOURCE_RECORDS_B64="([A-Za-z0-9+/=]+)"', mac_header)
    assert records_match is not None
    source_records = base64.b64decode(records_match.group(1), validate=True)
    assert source_records.endswith(b"\n")
    assert base64.b64encode(b"verification/lifecycle.py") in source_records

    malformed = put(tmp_path, "malformed.cmd", b"@echo off\n")
    with pytest.raises(LifecycleError, match="protected installer"):
        lifecycle._harden_built_installer(malformed)


@pytest.mark.skipif(os.name != "nt", reason="Windows one-click behavior")
def test_one_click_windows_installer_is_atomic_idempotent_and_preserves_trees(
    tmp_path: Path,
) -> None:
    _tar_payload, payload, _files = one_click_payloads(
        {"README.md": b"original\n"}
    )
    installer_dir = tmp_path / "installer artifacts with spaces"
    installer_dir.mkdir()
    installer = installer_dir / "SentiVue-Oracle-Setup-v1.2.3.cmd"
    installer.write_text(
        lifecycle._windows_installer_text(payload, hashlib.sha256(payload).hexdigest()),
        encoding="ascii",
    )

    destination = tmp_path / "installed tree with spaces"
    environment = dict(os.environ)
    environment.update(
        {
            "ORACLE_INSTALLER_DEST": str(destination),
            "ORACLE_INSTALLER_PROFILE": "micro",
            "ORACLE_INSTALLER_SKIP_SETUP": "1",
        }
    )
    first = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert (destination / "README.md").read_text(encoding="utf-8") == "original\n"
    marker = destination / ".oracle-installer-payload.sha256"
    assert (
        marker.read_text(encoding="utf-8").strip()
        == hashlib.sha256(payload).hexdigest()
    )
    assert not list(tmp_path.glob(".sentivue-oracle-install-*"))

    second = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "existing files were preserved" in second.stdout
    assert "resuming incomplete installation" in second.stdout
    assert not (destination / ".oracle-install-complete").exists()
    (destination / "README.md").write_text("locally modified\n", encoding="utf-8")
    modified = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert modified.returncode != 0
    assert "immutable source file" in modified.stdout
    assert (destination / "README.md").read_text(encoding="utf-8") == (
        "locally modified\n"
    )

    unowned = tmp_path / "unowned destination"
    unowned.mkdir()
    sentinel = put(unowned, "keep.txt", "preserve\n")
    refused_environment = dict(environment)
    refused_environment["ORACLE_INSTALLER_DEST"] = str(unowned)
    refused = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=refused_environment,
        check=False,
    )
    assert refused.returncode != 0
    assert "was preserved" in refused.stdout
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"

    tampered = installer_dir / "tampered.cmd"
    tampered_text = installer.read_text(encoding="ascii")
    separator = "#==B64PAYLOAD==#\n"
    header, encoded = tampered_text.split(separator, 1)
    replacement = "A" if encoded[0] != "A" else "B"
    tampered.write_text(
        header + separator + replacement + encoded[1:],
        encoding="ascii",
    )
    tampered_destination = tmp_path / "tampered destination"
    tampered_environment = dict(environment)
    tampered_environment["ORACLE_INSTALLER_DEST"] = str(tampered_destination)
    rejected = subprocess.run(
        ["cmd", "/d", "/c", str(tampered)],
        text=True,
        capture_output=True,
        env=tampered_environment,
        check=False,
    )
    assert rejected.returncode != 0
    assert "SHA-256 mismatch" in rejected.stdout
    assert not tampered_destination.exists()

    traversal_buffer = io.BytesIO()
    with zipfile.ZipFile(traversal_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sentivue-oracle/../../escaped.txt", "unsafe\n")
    traversal_payload = traversal_buffer.getvalue()
    with pytest.raises(LifecycleError, match="unsafe traversal|payload"):
        lifecycle._windows_installer_text(
            traversal_payload, hashlib.sha256(traversal_payload).hexdigest()
        )
    assert not (tmp_path / "escaped.txt").exists()

    rollback_destination = tmp_path / "profile rollback destination"
    rollback_environment = dict(environment)
    rollback_environment["ORACLE_INSTALLER_DEST"] = str(rollback_destination)
    rollback_environment["ORACLE_INSTALLER_PROFILE"] = "does-not-exist"
    rolled_back = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=rollback_environment,
        check=False,
    )
    assert rolled_back.returncode != 0
    assert "unknown model profile" in rolled_back.stdout
    assert not rollback_destination.exists()

    forged = tmp_path / "forged owned tree"
    put(
        forged,
        ".oracle-installer-payload.sha256",
        hashlib.sha256(payload).hexdigest() + "\n",
    )
    sentinel = tmp_path / "forged-child-ran.txt"
    put(
        forged,
        "bin/oracle.ps1",
        f"[IO.File]::WriteAllText('{sentinel}', 'unsafe')\nexit 0\n",
    )
    put(forged, "incoming/dependency-cache/manifest.json", "{}\n")
    forged_environment = dict(environment)
    forged_environment.pop("ORACLE_INSTALLER_SKIP_SETUP")
    forged_environment["ORACLE_INSTALLER_DEST"] = str(forged)
    forged_run = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=forged_environment,
        check=False,
    )
    assert forged_run.returncode != 0
    assert "immutable source file" in forged_run.stdout
    assert not sentinel.exists()

    extra_tree = tmp_path / "tree with unmanifested executable"
    extra_environment = dict(environment)
    extra_environment["ORACLE_INSTALLER_DEST"] = str(extra_tree)
    installed_extra_tree = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=extra_environment,
        check=False,
    )
    assert installed_extra_tree.returncode == 0
    extra_sentinel = tmp_path / "unmanifested-python-ran.txt"
    put(
        extra_tree,
        ".tools/bin/python.cmd",
        f'@echo off\r\necho unsafe>"{extra_sentinel}"\r\nexit /b 0\r\n',
    )
    extra_environment.pop("ORACLE_INSTALLER_SKIP_SETUP")
    refused_child_execution = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=extra_environment,
        check=False,
    )
    assert refused_child_execution.returncode != 0
    assert "resuming incomplete installation" in refused_child_execution.stdout
    assert "partial downloads were preserved" in refused_child_execution.stdout
    assert not extra_sentinel.exists()

    setup_sentinel = tmp_path / "partial-setup-side-effect.txt"
    escaped_sentinel = str(setup_sentinel).replace("'", "''")
    _tar_failure, failure_payload, _failure_files = one_click_payloads(
        {
            "bin/oracle.ps1": (
                f"[IO.File]::WriteAllText('{escaped_sentinel}', 'partial')\nexit 19\n"
            ).encode("ascii"),
            "bootstrap/uninstall.ps1": (
                "param([switch]$Apply,[string]$Root,[string]$HomePath)\n"
                f"Remove-Item -LiteralPath '{escaped_sentinel}' -Force -ErrorAction SilentlyContinue\n"
                "exit 0\n"
            ).encode("ascii"),
            "incoming/dependency-cache/manifest.json": b'{"schema_version":1,"artifacts":[]}\n',
        }
    )
    failure_installer = installer_dir / "setup-failure.cmd"
    failure_installer.write_text(
        lifecycle._windows_installer_text(
            failure_payload,
            hashlib.sha256(failure_payload).hexdigest(),
        ),
        encoding="ascii",
    )
    failure_destination = tmp_path / "setup failure destination"
    failure_environment = dict(environment)
    failure_environment.pop("ORACLE_INSTALLER_SKIP_SETUP")
    failure_environment["ORACLE_INSTALLER_DEST"] = str(failure_destination)
    failed_setup = subprocess.run(
        ["cmd", "/d", "/c", str(failure_installer)],
        text=True,
        capture_output=True,
        env=failure_environment,
        check=False,
    )
    assert failed_setup.returncode != 0
    assert "installer child setup failed" in failed_setup.stdout
    assert failure_destination.is_dir()
    assert "partial downloads were preserved" in failed_setup.stdout
    assert not (failure_destination / ".oracle-install-complete").exists()
    assert not setup_sentinel.exists(), failed_setup.stdout + failed_setup.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows one-click behavior")
@pytest.mark.parametrize(
    "profiles",
    [
        (
            b"full|2|fixture|fixture|fixture|fixture|0 GB\n"
            b"full|1|fixture|fixture|fixture|fixture|0 GB\n"
        ),
        b"full|invalid|fixture|fixture|fixture|fixture|0 GB\n",
        b"full|1|fixture|outside|fixture|fixture|0 GB\n",
        b"full|1|fixture,fixture|fixture|fixture|fixture|0 GB\n",
    ],
)
def test_windows_installer_rejects_malformed_or_ambiguous_profiles(
    tmp_path: Path,
    profiles: bytes,
) -> None:
    _tar_payload, payload, files = one_click_payloads(
        {"serving/profiles.conf": profiles}
    )
    installer = put(
        tmp_path,
        "malformed-profile.cmd",
        lifecycle._windows_installer_text(
            payload,
            hashlib.sha256(payload).hexdigest(),
            lifecycle._installer_source_manifest(files),
        ).encode("ascii"),
    )
    destination = tmp_path / "must not publish"
    environment = dict(os.environ)
    environment.update(
        {
            "ORACLE_INSTALLER_DEST": str(destination),
            "ORACLE_INSTALLER_PROFILE": "full",
            "ORACLE_INSTALLER_SKIP_SETUP": "1",
        }
    )

    result = subprocess.run(
        ["cmd", "/d", "/c", str(installer)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode != 0
    assert "serving profile" in result.stdout
    assert not destination.exists()


def test_one_click_builders_publish_native_unsigned_artifacts_honestly() -> None:
    windows = (REPO_ROOT / "bootstrap/build-one-click-installers.ps1").read_text(
        encoding="ascii"
    )
    mac = (REPO_ROOT / "bootstrap/build-one-click-installers.sh").read_text(
        encoding="utf-8"
    )
    package = (REPO_ROOT / "bootstrap/build-macos-package.sh").read_text(
        encoding="utf-8"
    )
    workflow = (REPO_ROOT / ".github/workflows/installers.yml").read_text(
        encoding="utf-8"
    )

    assert '"installers",' in windows
    assert '"--revision", $Revision' in windows
    assert "ARGS=(installers " in mac
    assert '"$ROOT/verification/lifecycle.py" "${ARGS[@]}"' in mac
    assert 'ORACLE_PYTHON="$PYTHON_BIN"' in mac
    assert "build-macos-package.sh" in mac
    assert "pkgbuild" in package
    assert "--nopayload" in package
    assert "ORACLE_INSTALLER_SKIP_SETUP=1" in package
    assert "checked-out package builder differs from immutable source" in package
    assert "base dependency sidecar is missing or invalid" in package
    assert 'if [[ "$DEPENDENCY_BUNDLE_NAME" != "-" ]]' in package
    assert '"code_signing": "unsigned"' in package
    assert '"notarization": "not-notarized"' in package
    assert "windows-latest" in workflow
    assert "macos-latest" in workflow
    assert "Require byte-identical cross-platform output" in workflow
    assert "Publish immutable GitHub release assets" in workflow
    assert "installer-package-macos" in workflow
    assert "installer-release-final" in workflow
    assert "RELEASE-SHA256SUMS" in workflow
    assert "Refuse public artifact publication" in workflow
    assert "ORACLE_DEPENDENCY_RELEASE_TAG" not in workflow
    assert "ORACLE_DEPENDENCY_ASSET_SHA256" not in workflow
    assert "ORACLE_RELEASE_DEPENDENCY_CACHE" not in workflow
    assert "downloads every checksum-bound dependency and model shard" in workflow
    assert "github.ref_name }}\"" not in workflow


def test_review_source_provenance_discloses_protected_builder_transform(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    add_package_policy(root, ["bootstrap"])
    put(root, "README.md", "fixture\n")
    builder = put(root, "bootstrap/build-installers.ps1", "# protected fixture\n")
    revision = init_repository(root)

    bundle = build_source_archives(root, revision, tmp_path / "out", "v1.2.3")
    with tarfile.open(bundle.archives[0], "r:gz") as archive:
        member = archive.extractfile("sentivue-oracle/SOURCE-PROVENANCE.json")
        assert member is not None
        provenance = json.loads(member.read().decode("utf-8"))

    disclosure = provenance["protected_builder_transform"]
    assert disclosure["id"] == lifecycle.INSTALLER_HARDENING_TRANSFORM
    assert (
        disclosure["source_sha256"] == hashlib.sha256(builder.read_bytes()).hexdigest()
    )
    assert disclosure["required_after_protected_builder"] is True
    assert "applied_after_protected_builder" not in disclosure


def test_review_posix_rendered_arguments_round_trip_shell_parsing() -> None:
    executable = "/Applications/Oracle Tools/bin/llama-server $(touch nope)"
    model = "/Volumes/Models & Data/model 'final'.gguf"
    rendered = " ".join(
        (
            lifecycle.quote_command_argument(executable, "posix"),
            "-m",
            lifecycle.quote_command_argument(model, "posix"),
        )
    )

    assert shlex.split(rendered) == [executable, "-m", model]


@pytest.mark.skipif(os.name != "nt", reason="uses the Windows command-line parser")
def test_review_windows_rendered_arguments_round_trip_command_line_to_argv() -> None:
    import ctypes

    executable = r"C:\Program Files\Oracle & Co\llama-server.exe"
    model = r"C:\Models & Data\model ^&! final.gguf"
    rendered = " ".join(
        (
            lifecycle.quote_command_argument(executable, "windows"),
            "-m",
            lifecycle.quote_command_argument(model, "windows"),
        )
    )
    argc = ctypes.c_int()
    parser = ctypes.windll.shell32.CommandLineToArgvW
    parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = parser(rendered, ctypes.byref(argc))
    try:
        parsed = [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)

    assert parsed == [executable, "-m", model]


def test_review_renderers_use_platform_argument_quoting() -> None:
    posix = (REPO_ROOT / "bootstrap/render-config.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    shared = (REPO_ROOT / "verification/serving.py").read_text(encoding="utf-8")

    assert "serving/service.sh" in posix
    assert "-m $ROOT/$path" not in posix
    assert "verification\\serving.py" in windows
    assert "$server $common -m $mp" not in windows
    assert "shlex.join(argv)" in shared
    assert "subprocess.list2cmdline(list(argv))" in shared


def model_authority_fixture(
    root: Path,
    *,
    content: bytes = b"authoritative model",
    revision: str = "a" * 40,
) -> Path:
    put(root, "VERSIONS.lock", "# fixture\n")
    put(
        root,
        "verification/policy.json",
        json.dumps({"dependency_inputs": []}) + "\n",
    )
    put(
        root,
        "serving/models.manifest",
        "# name | repo | include | slot | ctx | flags | revision\n"
        f"chat | example/chat | approved/*.gguf | fast | 32768 | | {revision}\n",
    )
    authority = {
        "schema_version": 1,
        "models": {
            "chat": {
                "repository": "example/chat",
                "revision": revision,
                "include": "approved/*.gguf",
                "files": [
                    {
                        "path": "approved/model.gguf",
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        },
    }
    serialized = json.dumps(authority, sort_keys=True) + "\n"
    put(
        root,
        "serving/model-authorities.json",
        serialized,
    )
    return put(root, "authority-input.json", serialized)


def promote_fixture_models(
    root: Path,
    cache: Path,
    specs: list[tuple[str, str, str, str, str, bytes]],
) -> None:
    authorities: dict[str, object] = {}
    for name, repository, include, revision, relative, content in specs:
        authorities[name] = {
            "repository": repository,
            "revision": revision,
            "include": include,
            "files": [
                {
                    "path": relative,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
    serialized = (
        json.dumps({"schema_version": 1, "models": authorities}, sort_keys=True) + "\n"
    )
    put(
        root,
        "serving/model-authorities.json",
        serialized,
    )
    authority_file = put(root, "authority-input.json", serialized)
    policy = root / "verification/policy.json"
    if not policy.is_file():
        put(root, "verification/policy.json", '{"dependency_inputs": []}\n')
    for name, _repository, _include, _revision, _relative, _content in specs:
        lifecycle.import_model_snapshot(
            root,
            cache,
            model_name=name,
            authority_file=authority_file,
        )


def test_second_review_local_model_scan_cannot_establish_trust(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cache = tmp_path / "cache"
    authority = model_authority_fixture(root)
    put(root, "models/chat/arbitrary.gguf", b"arbitrary")

    with pytest.raises(LifecycleError, match="include pattern"):
        record_model_snapshot(
            root,
            cache,
            model_name="chat",
            repository="example/chat",
            requested_revision="a" * 40,
            resolved_revision="a" * 40,
        )

    (root / "models/chat/arbitrary.gguf").unlink()
    put(
        root,
        "models/chat/approved/model.gguf",
        b"X" * len(b"authoritative model"),
    )
    evidence = record_model_snapshot(
        root,
        cache,
        model_name="chat",
        repository="example/chat",
        requested_revision="a" * 40,
        resolved_revision="a" * 40,
    )
    assert evidence.trust == "untrusted"
    assert any(
        "untrusted acquisition evidence" in error
        for error in validate_dependency_inputs(
            root,
            artifact_manifest=cache / "manifest.json",
            cache_root=cache,
            reproducible=True,
            artifact_ids={"model:chat"},
        )
    )

    with pytest.raises(LifecycleError, match="separate independently supplied"):
        lifecycle.import_model_snapshot(
            root,
            cache,
            model_name="chat",
            authority_file=root / "serving/model-authorities.json",
        )
    with pytest.raises(LifecycleError, match="checksum mismatch"):
        lifecycle.import_model_snapshot(
            root,
            cache,
            model_name="chat",
            authority_file=authority,
        )


def test_second_review_only_policy_bound_model_files_can_be_loaded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    cache = root / "incoming/dependency-cache"
    authority = model_authority_fixture(root)
    put(root, "models/chat/approved/model.gguf", b"authoritative model")
    put(
        root,
        "engines/claude-code/home/settings.json",
        '{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9099"}}\n',
    )
    put(
        root,
        "engines/opencode/xdg/opencode/opencode.json",
        '{"provider": {"oracle": {"models": {}}}}\n',
    )
    initialize_install_state(root, home, source_revision="c" * 40)

    with pytest.raises(LifecycleError, match="policy-bound model snapshot"):
        sync_model_configs(root, home)

    promoted = lifecycle.import_model_snapshot(
        root,
        cache,
        model_name="chat",
        authority_file=authority,
    )
    assert promoted.trust == "policy-bound"
    assert validate_model_snapshot(root, cache, promoted) == []
    assert sync_model_configs(root, home)
    assert lifecycle.validated_model_paths(root, cache, "chat") == [
        root / "models/chat/approved/model.gguf"
    ]

    put(root, "models/chat/approved/extra.gguf", b"not authorized")
    assert any(
        "unexpected model file" in error
        for error in validate_model_snapshot(root, cache, promoted)
    )
    with pytest.raises(LifecycleError, match="policy-bound model snapshot"):
        lifecycle.validated_model_paths(root, cache, "chat")


def test_second_review_renderers_resolve_only_validated_model_paths() -> None:
    posix = (REPO_ROOT / "bootstrap/render-config.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    shared = (REPO_ROOT / "verification/serving.py").read_text(encoding="utf-8")

    assert "serving/service.sh" in posix
    assert 'find "models/$1"' not in posix
    assert "verification\\serving.py" in windows
    assert 'Get-ChildItem $modelDir -Filter "*.gguf"' not in windows
    assert "validate_policy_bound_models" in shared
    assert "snapshots[name].paths" in shared


@pytest.mark.parametrize(
    "relative",
    [
        "harness/skill-packs/install-skill-packs.sh",
        "harness/skill-packs/install-skill-packs.ps1",
        "harness/agent-mcp/setup-agent-mcp.sh",
        "harness/agent-mcp/setup-agent-mcp.ps1",
        "harness/loop-engineering/install-loop-eng.sh",
        "harness/loop-engineering/install-loop-eng.ps1",
    ],
)
def test_second_review_optional_source_setups_use_validated_cache(
    relative: str,
) -> None:
    source = (REPO_ROOT / relative).read_text(encoding="utf-8")

    assert "git clone" not in source
    assert "winget install" not in source
    assert "install-source" in source
    assert "validate-source" in source
    if "loop-eng" in relative:
        assert "--offline" in source
        assert "@cobusgreyling/" not in source


def test_source_zip_materializes_internal_links_and_rejects_escapes(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("source-commit/CLAUDE.md", "authoritative\n")
        link = zipfile.ZipInfo("source-commit/AGENTS.md")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "CLAUDE.md")

    extracted = lifecycle._extract_source_archive(archive, tmp_path / "safe")

    alias = extracted / "AGENTS.md"
    assert alias.read_text(encoding="utf-8") == "authoritative\n"
    assert not alias.is_symlink()

    escaping = tmp_path / "escaping.zip"
    with zipfile.ZipFile(escaping, "w") as output:
        output.writestr("outside.txt", "unsafe\n")
        link = zipfile.ZipInfo("source-commit/AGENTS.md")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "../outside.txt")
    with pytest.raises(LifecycleError, match="escapes its source root"):
        lifecycle._extract_source_archive(escaping, tmp_path / "rejected")


def test_second_review_source_install_rejects_modified_existing_checkout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cache = tmp_path / "cache"
    destination = root / "harness/vendor/source"
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("source-commit/tool.txt", "authoritative\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    revision = "b" * 40
    put(
        root,
        "VERSIONS.lock",
        f"SOURCE_PIN={revision}\n"
        f"SOURCE_COMMIT={revision}\n"
        f"SOURCE_SHA256={digest}\n"
        "SOURCE_REPO=https://example.invalid/source\n",
    )
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "source-test",
                        "kind": "git",
                        "version_key": "SOURCE_PIN",
                        "allow_dynamic": False,
                        "source": {
                            "identity_key": "SOURCE_REPO",
                            "url": "{identity}/archive/{resolved}.zip",
                            "revision_key": "SOURCE_COMMIT",
                            "digest_key": "SOURCE_SHA256",
                        },
                    }
                ]
            }
        )
        + "\n",
    )
    put(root, "serving/models.manifest", "# no models\n")
    lifecycle.import_artifact(
        root,
        cache,
        artifact_id="source-test",
        source_file=archive,
        source_url=f"https://example.invalid/source/archive/{revision}.zip",
        requested_version=revision,
        resolved_version=revision,
    )
    lifecycle.install_source_archive(
        root,
        cache / "manifest.json",
        cache,
        "source-test",
        destination,
        trusted_root=root,
        expected_version=revision,
        expected_requested_version=revision,
    )
    assert (
        lifecycle.validate_source_install(
            root,
            cache / "manifest.json",
            cache,
            "source-test",
            destination,
            trusted_root=root,
            expected_version=revision,
            expected_requested_version=revision,
        )
        == []
    )

    (destination / "tool.txt").write_text("locally changed\n", encoding="utf-8")
    assert any(
        "installed source tree digest mismatch" in error
        for error in lifecycle.validate_source_install(
            root,
            cache / "manifest.json",
            cache,
            "source-test",
            destination,
            trusted_root=root,
            expected_version=revision,
            expected_requested_version=revision,
        )
    )


def test_second_review_optional_oci_archives_have_an_import_authority_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cache = tmp_path / "cache"
    archive = put(tmp_path, "image.tar", b"offline OCI archive")
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    image_digest = "sha256:" + ("d" * 64)
    put(
        root,
        "VERSIONS.lock",
        "IMAGE=registry.example.invalid/db:1.0\n"
        f"IMAGE_DIGEST={image_digest}\n"
        f"IMAGE_ARCHIVE_SHA256={archive_digest}\n",
    )
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "container-db",
                        "kind": "container",
                        "version_key": "IMAGE",
                        "allow_dynamic": False,
                        "optional": True,
                        "platforms": ["docker"],
                        "source": {
                            "identity": "oci://{version}@{identity_digest}",
                            "identity_digest_key": "IMAGE_DIGEST",
                            "artifact_digest_key": "IMAGE_ARCHIVE_SHA256",
                        },
                    }
                ]
            }
        )
        + "\n",
    )
    put(root, "serving/models.manifest", "# no models\n")

    assert validate_dependency_inputs(root, reproducible=True) == []
    optional_errors = validate_dependency_inputs(
        root, reproducible=True, include_optional=True
    )
    assert any("resolved artifact" in error for error in optional_errors)

    record = lifecycle.import_artifact(
        root,
        cache,
        artifact_id="container-db",
        source_file=archive,
        source_url=f"oci://registry.example.invalid/db:1.0@{image_digest}",
        requested_version="registry.example.invalid/db:1.0",
        resolved_version="registry.example.invalid/db:1.0",
    )
    assert record.trust == "policy-bound"
    assert (
        validate_dependency_inputs(
            root,
            artifact_manifest=cache / "manifest.json",
            cache_root=cache,
            reproducible=True,
            include_optional=True,
        )
        == []
    )


def test_second_review_install_resumes_after_completed_profile_under_set_u(
    tmp_path: Path,
) -> None:
    bash = (
        Path("C:/Program Files/Git/bin/bash.exe")
        if os.name == "nt"
        else Path(shutil.which("bash") or "")
    )
    if not bash.is_file():
        pytest.skip("Bash is unavailable")
    root = tmp_path / "resume root"
    home = tmp_path / "home"
    fake_bin = root / "fake-bin"
    root.mkdir()
    home.mkdir()
    fake_bin.mkdir()
    shutil.copy2(REPO_ROOT / "install", root / "install")
    put(root, "serving/models.profile", "chat\n")
    put(
        root,
        "serving/models.manifest",
        "chat | example/chat | model.gguf | fast | 8192 | | " + ("e" * 40) + "\n",
    )
    put(
        root,
        "serving/tiers.env",
        "OPUS_MODEL=chat\nSONNET_MODEL=chat\nHAIKU_MODEL=chat\n",
    )
    put(root, "models/chat/model.gguf", b"model")
    put(
        root,
        "bootstrap/download-models.sh",
        "#!/usr/bin/env bash\nexit 0\n",
    ).chmod(0o755)
    fake_python = put(
        fake_bin,
        "python3",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then exit 0; fi
case " $* " in
  *" state phase-current "*" --phase models "*) exit 1 ;;
  *) exit 0 ;;
esac
""",
    )
    fake_curl = put(fake_bin, "curl", "#!/usr/bin/env bash\nexit 0\n")
    fake_df = put(
        fake_bin,
        "df",
        "#!/usr/bin/env bash\n"
        "printf 'Filesystem Blocks Used Available Capacity Mounted\\n"
        "fixture 1000 1 999 1%% /\\n'\n",
    )
    for path in (fake_python, fake_curl, fake_df):
        path.chmod(0o755)
    put(
        root,
        "connectors/ide/sync-models.sh",
        '#!/usr/bin/env bash\nmkdir -p "$PWD/state/generated"\n',
    ).chmod(0o755)
    put(
        root,
        "bootstrap/render-config.sh",
        '#!/usr/bin/env bash\nmkdir -p "$PWD/serving"; : > "$PWD/serving/llama-swap.rendered.yaml"\n',
    ).chmod(0o755)
    put(
        root,
        "serving/service.sh",
        '#!/usr/bin/env bash\nmkdir -p "$PWD/logs" "$HOME/Library/LaunchAgents"; '
        ': > "$HOME/Library/LaunchAgents/com.sentivue.llamaswap.plist"\n',
    ).chmod(0o755)
    put(root, "bootstrap/verify-offline.sh", "#!/usr/bin/env bash\nexit 0\n").chmod(
        0o755
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]

    completed = subprocess.run(
        [str(bash), str(root / "install"), "--yes"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_second_review_model_ownership_excludes_unselected_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    authority = model_authority_fixture(root)
    selected = put(root, "models/chat/approved/model.gguf", b"authoritative model")
    initialize_install_state(root, home, source_revision="f" * 40)
    lifecycle.import_model_snapshot(
        root,
        cache,
        model_name="chat",
        authority_file=authority,
    )
    begin_install_phase(root, "models")
    lifecycle.register_model_ownership(root, home, cache, "chat")
    mark_install_phase(root, "models")
    user_file = put(root, "models/user-copy.gguf", b"user")

    uninstall(root, home, apply=True)

    assert not selected.exists()
    assert user_file.read_bytes() == b"user"


def test_second_review_install_state_rejects_reparse_directory_and_final_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    state_directory = root / ".install-state"
    original = lifecycle._is_reparse_point
    monkeypatch.setattr(
        lifecycle,
        "_is_reparse_point",
        lambda path: Path(path) == state_directory or original(Path(path)),
    )
    with pytest.raises(LifecycleError, match="reparse"):
        initialize_install_state(root, home, source_revision="1" * 40)
    assert not (state_directory / "state.json").exists()

    monkeypatch.setattr(lifecycle, "_is_reparse_point", original)
    initialize_install_state(root, home, source_revision="1" * 40)
    state_file = state_directory / "state.json"
    monkeypatch.setattr(
        lifecycle,
        "_is_reparse_point",
        lambda path: Path(path) == state_file or original(Path(path)),
    )
    with pytest.raises(LifecycleError, match="reparse"):
        begin_install_phase(root, "bootstrap")


def test_second_review_state_mutations_check_lexical_root_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "real-root"
    home = tmp_path / "home"
    unsafe_alias = tmp_path / "junction-root"
    initialize_install_state(root, home, source_revision="3" * 40)
    original_resolve = Path.resolve
    original_is_reparse = lifecycle._is_reparse_point

    def fake_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if self == unsafe_alias:
            return root
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    monkeypatch.setattr(
        lifecycle,
        "_is_reparse_point",
        lambda path: Path(path) == unsafe_alias or original_is_reparse(Path(path)),
    )

    with pytest.raises(LifecycleError, match="reparse"):
        begin_install_phase(unsafe_alias, "bootstrap")


def test_second_review_transformed_installer_writer_checks_reparse_paths(
    tmp_path: Path,
) -> None:
    _tar_payload, zip_payload, _files = one_click_payloads()
    old_installer = """@echo off
#==PSPAYLOAD==#
$ErrorActionPreference = "Stop"
if ($sel.Name -eq "full") { Remove-Item (Join-Path $dest "serving\\models.profile") -ErrorAction SilentlyContinue }
else { Set-Content -Path (Join-Path $dest "serving\\models.profile") -Value (($sel.Models -split ",") -join "`n") }
Set-Content -Path (Join-Path $dest "serving\\tiers.env") -Value @("OPUS_MODEL=$($sel.Opus)", "SONNET_MODEL=$($sel.Sonnet)", "HAIKU_MODEL=$($sel.Haiku)")
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "connectors\\ide\\setup-ide.ps1") install
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\\download-models.ps1")
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bin\\oracle.ps1") setup
#==B64PAYLOAD==#
__PAYLOAD__
""".replace("__PAYLOAD__", base64.b64encode(zip_payload).decode("ascii"))
    windows = put(
        tmp_path,
        "installer.cmd",
        old_installer,
    )
    lifecycle._harden_built_installer(windows)
    hardened = windows.read_text(encoding="ascii")
    helper = lifecycle._installer_atomic_helper()

    assert "FileAttributes]::ReparsePoint" in hardened
    assert hardened.count("Assert-SafeAtomicPath") >= 4
    assert "Assert-SafeAtomicPath -Path $Path" in helper

    if os.name != "nt":
        return
    outside = tmp_path / "outside"
    junction = tmp_path / "junction"
    outside.mkdir()
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        text=True,
        capture_output=True,
        check=False,
    )
    if linked.returncode != 0:
        pytest.skip(f"junctions unavailable: {linked.stderr}")
    probe = put(
        tmp_path,
        "probe.ps1",
        helper
        + "\nWrite-Utf8NoBomAtomic -Path "
        + repr(str(junction / "victim.txt"))
        + " -Value 'unsafe'\n",
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "reparse" in (completed.stdout + completed.stderr).lower()
    assert not (outside / "victim.txt").exists()


def test_second_review_preflight_rebuilds_before_reusing_local_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    output = tmp_path / "out"
    root.mkdir()
    output.mkdir()
    revision = "2" * 40
    existing_paths = [
        put(output, "source.zip", b"tampered"),
        put(output, "installer.command", b"mac"),
        put(output, "installer.cmd", b"windows"),
    ]
    existing = lifecycle.ReleaseBundle(
        version="v1.2.3",
        revision=revision,
        output_dir=output,
        archives=existing_paths,
        checksums=put(output, "SHA256SUMS", b"self-authored"),
        provenance=put(output, "PROVENANCE.json", b"{}"),
    )
    monkeypatch.setattr(lifecycle, "_resolve_revision", lambda *_args: revision)
    monkeypatch.setattr(lifecycle, "_current_revision", lambda *_args: revision)
    monkeypatch.setattr(
        lifecycle, "_require_release_inputs_match_revision", lambda *_args: None
    )
    monkeypatch.setattr(lifecycle, "validate_dependency_inputs", lambda *_a, **_k: [])
    monkeypatch.setattr(lifecycle, "verify_release_bundle", lambda *_args: existing)
    builds: list[Path] = []

    def build_expected(
        _root: Path,
        _version: str,
        destination: Path,
        _revision: str,
        dependency_cache: Path | None = None,
    ) -> lifecycle.ReleaseBundle:
        assert dependency_cache == root / "incoming" / "dependency-cache"
        builds.append(destination)
        archives = [
            put(destination, "source.zip", b"authoritative"),
            put(destination, "installer.command", b"mac"),
            put(destination, "installer.cmd", b"windows"),
        ]
        return lifecycle.ReleaseBundle(
            version="v1.2.3",
            revision=revision,
            output_dir=destination,
            archives=archives,
            checksums=put(destination, "SHA256SUMS", b"expected"),
            provenance=put(destination, "PROVENANCE.json", b"expected provenance"),
        )

    monkeypatch.setattr(
        lifecycle, "_build_release_bundle", build_expected, raising=False
    )
    with pytest.raises(LifecycleError, match="immutable rebuild"):
        lifecycle.preflight_release(root, "v1.2.3", output, revision)
    assert len(builds) == 1
    assert builds[0] != output


@pytest.mark.parametrize("remote_tag_exists", [False, True])
def test_review_publication_resumes_tag_only_and_pushed_tag_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_tag_exists: bool,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    revision = "e" * 40
    asset = put(output, "asset.zip", b"asset")
    checksums = put(output, "SHA256SUMS", "fixture\n")
    provenance = put(output, "PROVENANCE.json", "{}\n")
    bundle = lifecycle.ReleaseBundle(
        version="v1.2.3",
        revision=revision,
        output_dir=output,
        archives=[asset],
        checksums=checksums,
        provenance=provenance,
    )
    monkeypatch.setattr(lifecycle, "preflight_release", lambda *_a, **_k: bundle)
    monkeypatch.setattr(lifecycle, "verify_release_bundle", lambda *_a, **_k: bundle)
    commands: list[list[str]] = []

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, revision + "\n", "")
        if argv[:2] == ["git", "ls-remote"]:
            if remote_tag_exists:
                return subprocess.CompletedProcess(
                    argv, 0, f"{revision}\trefs/tags/v1.2.3\n", ""
                )
            return subprocess.CompletedProcess(argv, 2, "", "")
        if argv[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)

    publish_release(root, "v1.2.3", output)

    assert not any(command[:2] == ["git", "tag"] for command in commands)
    pushes = [command for command in commands if command[:2] == ["git", "push"]]
    assert bool(pushes) is (not remote_tag_exists)
    assert not any(command[:2] == ["gh", "release"] for command in commands)
    assert not any(
        "--force" in command or "--clobber" in command or "delete" in command
        for command in commands
    )


def test_review_release_preflight_reuses_complete_verified_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    revision = "f" * 40
    artifacts = [
        put(output, "sentivue-oracle-v1.2.3.tar.gz", b"source"),
        put(output, "sentivue-oracle-v1.2.3.zip", b"source"),
        put(output, "SentiVue-Oracle-Installer-v1.2.3.command", b"installer"),
        put(output, "SentiVue-Oracle-Setup-v1.2.3.cmd", b"installer"),
    ]
    bundle = lifecycle.ReleaseBundle(
        version="v1.2.3",
        revision=revision,
        output_dir=output,
        archives=artifacts,
        checksums=put(output, "SHA256SUMS", "verified\n"),
        provenance=put(output, "PROVENANCE.json", "{}\n"),
    )
    monkeypatch.setattr(lifecycle, "_resolve_revision", lambda *_a: revision)
    monkeypatch.setattr(lifecycle, "_current_revision", lambda *_a: revision)
    monkeypatch.setattr(
        lifecycle, "_require_release_inputs_match_revision", lambda *_a: None
    )
    monkeypatch.setattr(lifecycle, "validate_dependency_inputs", lambda *_a, **_k: [])
    monkeypatch.setattr(lifecycle, "verify_release_bundle", lambda *_a: bundle)
    rebuilt: list[Path] = []

    def rebuild(
        _root: Path,
        _version: str,
        destination: Path,
        _revision: str,
        dependency_cache: Path | None = None,
    ) -> lifecycle.ReleaseBundle:
        assert dependency_cache == root / "incoming" / "dependency-cache"
        rebuilt.append(destination)
        expected_archives = [
            put(destination, path.name, path.read_bytes()) for path in artifacts
        ]
        return lifecycle.ReleaseBundle(
            version="v1.2.3",
            revision=revision,
            output_dir=destination,
            archives=expected_archives,
            checksums=put(destination, "SHA256SUMS", bundle.checksums.read_bytes()),
            provenance=put(
                destination, "PROVENANCE.json", bundle.provenance.read_bytes()
            ),
        )

    monkeypatch.setattr(lifecycle, "_build_release_bundle", rebuild)

    resumed = lifecycle.preflight_release(root, "v1.2.3", output, revision)

    assert resumed == bundle
    assert len(rebuilt) == 1
    assert rebuilt[0] != output


def test_review_release_rejects_dirty_dependency_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    add_package_policy(root, ["serving", "env"])
    put(root, "README.md", "fixture\n")
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(root, "serving/models.manifest", "# fixture\n")
    put(root, "env/uv.lock", "version = 1\nrevision = 3\n")
    revision = init_repository(root)
    (root / "VERSIONS.lock").write_text("TOOL_VERSION=attacker\n", encoding="utf-8")

    with pytest.raises(LifecycleError, match="differs from immutable revision"):
        lifecycle._require_release_inputs_match_revision(root, revision)


def source_install_fixture(
    tmp_path: Path,
    *,
    revision: str,
    content: bytes,
) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "policy root"
    trusted = root / "managed source trees"
    cache = tmp_path / "offline cache"
    archive = tmp_path / f"source-{revision[0]}.zip"
    trusted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("source-export/tool.txt", content)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    put(
        root,
        "VERSIONS.lock",
        f"SOURCE_PIN={revision}\n"
        f"SOURCE_COMMIT={revision}\n"
        f"SOURCE_SHA256={digest}\n"
        "SOURCE_REPO=https://example.invalid/source\n",
    )
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "source-test",
                        "kind": "git",
                        "version_key": "SOURCE_PIN",
                        "allow_dynamic": False,
                        "source": {
                            "identity_key": "SOURCE_REPO",
                            "url": "{identity}/archive/{resolved}.zip",
                            "revision_key": "SOURCE_COMMIT",
                            "digest_key": "SOURCE_SHA256",
                        },
                    }
                ]
            }
        )
        + "\n",
    )
    put(root, "serving/models.manifest", "# no models\n")
    lifecycle.import_artifact(
        root,
        cache,
        artifact_id="source-test",
        source_file=archive,
        source_url=f"https://example.invalid/source/archive/{revision}.zip",
        requested_version=revision,
        resolved_version=revision,
    )
    return root, trusted, cache, archive


def install_fixture_source(
    root: Path,
    trusted: Path,
    cache: Path,
    destination: Path,
    revision: str,
) -> Path:
    lifecycle.preflight_source_install(
        root,
        cache / "manifest.json",
        cache,
        "source-test",
        destination,
        trusted_root=trusted,
        expected_version=revision,
        expected_requested_version=revision,
    )
    return lifecycle.install_source_archive(
        root,
        cache / "manifest.json",
        cache,
        "source-test",
        destination,
        trusted_root=trusted,
        expected_version=revision,
        expected_requested_version=revision,
    )


def test_third_review_source_install_confines_lexical_paths_and_supports_spaces(
    tmp_path: Path,
) -> None:
    revision = "a" * 40
    root, trusted, cache, _archive = source_install_fixture(
        tmp_path, revision=revision, content=b"version one\n"
    )
    destination = trusted / "component with spaces"

    installed = install_fixture_source(root, trusted, cache, destination, revision)

    assert installed == destination
    assert (destination / "tool.txt").read_bytes() == b"version one\n"
    escape = trusted / ".." / "outside" / "component"
    with pytest.raises(LifecycleError, match="trusted root|lexical traversal"):
        install_fixture_source(root, trusted, cache, escape, revision)
    assert not (tmp_path / "policy root" / "outside").exists()


def test_third_review_source_install_rejects_symlink_ancestor(
    tmp_path: Path,
) -> None:
    revision = "a" * 40
    root, trusted, cache, _archive = source_install_fixture(
        tmp_path, revision=revision, content=b"version one\n"
    )
    outside = tmp_path / "outside"
    linked = trusted / "linked"
    outside.mkdir()
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(LifecycleError, match="reparse|symbolic"):
        install_fixture_source(root, trusted, cache, linked / "component", revision)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("unsafe_part", ["junction", "component"])
def test_third_review_source_install_rejects_simulated_windows_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_part: str,
) -> None:
    revision = "a" * 40
    root, trusted, cache, _archive = source_install_fixture(
        tmp_path, revision=revision, content=b"version one\n"
    )
    ancestor = trusted / "junction"
    destination = ancestor / "component"
    ancestor.mkdir()
    original = lifecycle._is_reparse_point
    unsafe = ancestor if unsafe_part == "junction" else destination
    monkeypatch.setattr(
        lifecycle,
        "_is_reparse_point",
        lambda path: Path(path) == unsafe or original(Path(path)),
    )

    with pytest.raises(LifecycleError, match="reparse"):
        install_fixture_source(root, trusted, cache, destination, revision)

    assert not destination.exists()
    assert not any(path.name.startswith(".component") for path in ancestor.iterdir())


def test_third_review_source_install_preserves_unowned_and_modified_trees(
    tmp_path: Path,
) -> None:
    first_revision = "a" * 40
    root, trusted, cache, _archive = source_install_fixture(
        tmp_path, revision=first_revision, content=b"version one\n"
    )
    unowned = trusted / "unowned"
    put(unowned, "user.txt", b"user data")
    with pytest.raises(LifecycleError, match="unowned"):
        install_fixture_source(root, trusted, cache, unowned, first_revision)
    assert (unowned / "user.txt").read_bytes() == b"user data"

    destination = trusted / "owned"
    install_fixture_source(root, trusted, cache, destination, first_revision)
    (destination / "tool.txt").write_bytes(b"locally modified\n")
    second_revision = "b" * 40
    source_install_fixture(tmp_path, revision=second_revision, content=b"version two\n")
    with pytest.raises(LifecycleError, match="modified"):
        install_fixture_source(root, trusted, cache, destination, second_revision)
    assert (destination / "tool.txt").read_bytes() == b"locally modified\n"


def test_third_review_source_install_upgrades_idempotently_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_revision = "a" * 40
    root, trusted, cache, _archive = source_install_fixture(
        tmp_path, revision=first_revision, content=b"version one\n"
    )
    destination = trusted / "component with spaces"
    install_fixture_source(root, trusted, cache, destination, first_revision)

    second_revision = "b" * 40
    source_install_fixture(tmp_path, revision=second_revision, content=b"version two\n")
    install_fixture_source(root, trusted, cache, destination, second_revision)
    assert (destination / "tool.txt").read_bytes() == b"version two\n"
    receipt_before = (destination / lifecycle.SOURCE_RECEIPT).read_bytes()
    tool_mtime = (destination / "tool.txt").stat().st_mtime_ns
    install_fixture_source(root, trusted, cache, destination, second_revision)
    assert (destination / lifecycle.SOURCE_RECEIPT).read_bytes() == receipt_before
    assert (destination / "tool.txt").stat().st_mtime_ns == tool_mtime

    third_revision = "c" * 40
    source_install_fixture(
        tmp_path, revision=third_revision, content=b"version three\n"
    )
    original_replace = lifecycle.os.replace
    failed = False

    def fail_new_tree_once(source: object, target: object) -> None:
        nonlocal failed
        if Path(target) == destination and not failed:
            failed = True
            raise OSError("simulated replacement failure")
        original_replace(source, target)

    monkeypatch.setattr(lifecycle.os, "replace", fail_new_tree_once)
    with pytest.raises(OSError, match="simulated replacement"):
        install_fixture_source(root, trusted, cache, destination, third_revision)
    assert (destination / "tool.txt").read_bytes() == b"version two\n"
    lifecycle.preflight_source_install(
        root,
        cache / "manifest.json",
        cache,
        "source-test",
        destination,
        trusted_root=trusted,
        expected_version=third_revision,
        expected_requested_version=third_revision,
    )


def test_final_task2_failed_upgrade_cleans_only_restored_empty_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_revision = "a" * 40
    root, trusted, cache, _archive = source_install_fixture(
        tmp_path, revision=first_revision, content=b"version one\n"
    )
    destination = trusted / "component"
    install_fixture_source(root, trusted, cache, destination, first_revision)
    second_revision = "b" * 40
    source_install_fixture(tmp_path, revision=second_revision, content=b"version two\n")
    original_replace = lifecycle.os.replace
    failed = False

    def fail_upgrade_once(source: object, target: object) -> None:
        nonlocal failed
        if Path(target) == destination and not failed:
            failed = True
            raise OSError("simulated upgrade failure")
        original_replace(source, target)

    monkeypatch.setattr(lifecycle.os, "replace", fail_upgrade_once)
    with pytest.raises(OSError, match="simulated upgrade failure"):
        install_fixture_source(root, trusted, cache, destination, second_revision)

    assert (destination / "tool.txt").read_bytes() == b"version one\n"
    backup_pattern = f".{destination.name}.backup-*"
    assert list(destination.parent.glob(backup_pattern)) == []

    def fail_upgrade_and_rollback(source: object, target: object) -> None:
        if Path(target) == destination:
            if Path(source).name == "previous":
                raise OSError("simulated rollback failure")
            raise OSError("simulated upgrade failure")
        original_replace(source, target)

    monkeypatch.setattr(
        lifecycle.os,
        "replace",
        fail_upgrade_and_rollback,
    )
    with pytest.raises(OSError, match="simulated rollback failure"):
        install_fixture_source(root, trusted, cache, destination, second_revision)

    backups = list(destination.parent.glob(backup_pattern))
    assert len(backups) == 1
    assert (backups[0] / "previous" / "tool.txt").read_bytes() == b"version one\n"
    assert not destination.exists()


@pytest.mark.parametrize(
    ("kind", "requested", "resolved", "source_url"),
    [
        ("npm", "1.2.3", "1.2.3", "https://registry.invalid/pkg-1.2.3.tgz"),
        (
            "git",
            "v1.2.3",
            "d" * 40,
            "https://example.invalid/repo/archive/" + ("d" * 40) + ".zip",
        ),
        ("native", "unresolved", "2.3.4", "https://example.invalid/native.bin"),
        ("toolchain", "0.11.26", "0.11.26", "https://example.invalid/uv.tar.gz"),
        ("ide", "dynamic", "1.99.0", "https://example.invalid/ide.zip"),
        (
            "ide-extension",
            "dynamic",
            "1.5.0",
            "https://example.invalid/extension.vsix",
        ),
        (
            "python",
            "package==1.0.0",
            "package==1.0.0",
            "https://example.invalid/package.whl",
        ),
        (
            "container",
            "registry.invalid/image:1.0",
            "registry.invalid/image:1.0",
            "oci://registry.invalid/image:1.0@sha256:" + ("e" * 64),
        ),
    ],
)
def test_third_review_generic_authority_promotion_round_trips_each_kind(
    tmp_path: Path,
    kind: str,
    requested: str,
    resolved: str,
    source_url: str,
) -> None:
    root = tmp_path / f"{kind} policy"
    cache = tmp_path / f"{kind} cache"
    artifact_id = f"{kind}-fixture"
    source_file = put(tmp_path, f"{kind}.artifact", f"{kind} bytes\n")
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    dynamic = requested in {"dynamic", "unresolved"}
    version_lines = [
        f"ARTIFACT_VERSION={requested}",
        "ARTIFACT_SHA256=unresolved",
    ]
    source: dict[str, str] = {
        "identity": "unresolved",
        "digest_key": "ARTIFACT_SHA256",
    }
    if dynamic:
        version_lines.append("ARTIFACT_RESOLVED=unresolved")
        source["resolved_version_key"] = "ARTIFACT_RESOLVED"
    if kind == "git":
        version_lines.append("ARTIFACT_COMMIT=unresolved")
        source["revision_key"] = "ARTIFACT_COMMIT"
    if kind == "container":
        version_lines.extend(
            [
                "ARTIFACT_IDENTITY_DIGEST=unresolved",
                "ARTIFACT_ARCHIVE_SHA256=unresolved",
            ]
        )
        source = {
            "identity": "oci://{version}@{identity_digest}",
            "identity_digest_key": "ARTIFACT_IDENTITY_DIGEST",
            "artifact_digest_key": "ARTIFACT_ARCHIVE_SHA256",
        }
    put(root, "VERSIONS.lock", "\n".join(version_lines) + "\n")
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": artifact_id,
                        "kind": kind,
                        "version_key": "ARTIFACT_VERSION",
                        "allow_dynamic": dynamic,
                        "source": source,
                    }
                ]
            }
        )
        + "\n",
    )
    put(root, "serving/models.manifest", "# no models\n")
    authority = put(
        tmp_path,
        f"{kind}-authority.json",
        json.dumps(
            {
                "schema_version": 1,
                "authorities": {
                    artifact_id: {
                        "kind": kind,
                        "requested_version": requested,
                        "resolved_version": resolved,
                        "source_url": source_url,
                        "sha256": digest,
                    }
                },
            }
        )
        + "\n",
    )

    before = validate_dependency_inputs(root, reproducible=True)
    assert any("unresolved" in error for error in before)
    promoted = lifecycle.promote_dependency_authority(
        root, artifact_id=artifact_id, authority_file=authority
    )
    assert promoted["sha256"] == digest
    tracked = root / "verification" / "dependency-authorities.json"
    with pytest.raises(LifecycleError, match="independently supplied"):
        lifecycle.promote_dependency_authority(
            root, artifact_id=artifact_id, authority_file=tracked
        )

    record = lifecycle.import_artifact(
        root,
        cache,
        artifact_id=artifact_id,
        source_file=source_file,
        source_url=source_url,
        requested_version=requested,
        resolved_version=resolved,
    )
    assert record.trust == "policy-bound"
    assert (
        validate_dependency_inputs(
            root,
            artifact_manifest=cache / "manifest.json",
            cache_root=cache,
            reproducible=True,
        )
        == []
    )

    source_file.write_bytes(b"self-hashed replacement")
    with pytest.raises(LifecycleError, match="trusted digest"):
        lifecycle.import_artifact(
            root,
            cache,
            artifact_id=artifact_id,
            source_file=source_file,
            source_url=source_url,
            requested_version=requested,
            resolved_version=resolved,
        )


def test_third_review_promotion_rejects_incomplete_expected_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    put(
        root,
        "VERSIONS.lock",
        "TOOL_VERSION=unresolved\nTOOL_RESOLVED=unresolved\nTOOL_SHA256=unresolved\n",
    )
    put(
        root,
        "verification/policy.json",
        json.dumps(
            {
                "dependency_inputs": [
                    {
                        "id": "tool",
                        "kind": "toolchain",
                        "version_key": "TOOL_VERSION",
                        "allow_dynamic": True,
                        "source": {
                            "identity": "unresolved",
                            "resolved_version_key": "TOOL_RESOLVED",
                            "digest_key": "TOOL_SHA256",
                        },
                    }
                ]
            }
        )
        + "\n",
    )
    authority = put(
        tmp_path,
        "incomplete-authority.json",
        json.dumps(
            {
                "schema_version": 1,
                "authorities": {
                    "tool": {
                        "kind": "toolchain",
                        "requested_version": "unresolved",
                        "resolved_version": "1.0.0",
                        "source_url": "https://example.invalid/tool.zip",
                        "sha256": "unresolved",
                    }
                },
            }
        )
        + "\n",
    )

    with pytest.raises(LifecycleError, match="SHA-256"):
        lifecycle.promote_dependency_authority(
            root, artifact_id="tool", authority_file=authority
        )
    assert not (root / "verification/dependency-authorities.json").exists()


def test_third_review_release_rebuild_is_time_deterministic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    add_package_policy(root, ["src"])
    put(root, "README.md", "fixture\n")
    put(root, "src/main.py", "VALUE = 1\n")
    revision = init_repository(root)
    first = build_source_archives(root, revision, tmp_path / "first output", "v1.2.3")
    time.sleep(0.02)
    second = build_source_archives(root, revision, tmp_path / "second output", "v1.2.3")

    first_files = {
        path.name: path.read_bytes()
        for path in [*first.archives, first.provenance, first.checksums]
    }
    second_files = {
        path.name: path.read_bytes()
        for path in [*second.archives, second.provenance, second.checksums]
    }
    assert first_files == second_files
    source_epoch = int(
        subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%ct", revision],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    )
    expected_created = (
        lifecycle.datetime.fromtimestamp(source_epoch, lifecycle.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    provenance = json.loads(first.provenance.read_text(encoding="utf-8"))
    assert provenance["created_at"] == expected_created


def test_third_review_installer_synthetic_commit_uses_source_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    add_package_policy(root, ["bootstrap", "verification"])
    put(root, "README.md", "fixture\n")
    put(root, "bootstrap/build-installers.ps1", "# protected fixture\n")
    put(root, "verification/lifecycle.py", "# lifecycle fixture\n")
    revision = init_repository(root)
    output = tmp_path / "output"
    output.mkdir()
    source_epoch = lifecycle._git_timestamp(root, revision)
    original_checked = lifecycle._checked
    invocations: list[tuple[list[str], dict[str, str] | None]] = []

    def checked(
        argv: list[str],
        *,
        cwd: Path,
        text: bool = True,
        input_data: str | bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[object]:
        invocations.append((list(argv), env))
        if argv[0] == "fixture-powershell":
            built = Path(argv[argv.index("-OutDir") + 1])
            put(
                built,
                "SentiVue-Oracle-Installer-v1.2.3.command",
                b"#!/bin/bash\nbash install || true\n__PAYLOAD_BELOW__\nold",
            )
            put(
                built,
                "SentiVue-Oracle-Setup-v1.2.3.cmd",
                b"@echo off\r\n#==B64PAYLOAD==#\r\nb2xk\r\n",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        return original_checked(
            argv, cwd=cwd, text=text, input_data=input_data, env=env
        )

    monkeypatch.setattr(lifecycle, "_checked", checked)
    monkeypatch.setattr(lifecycle, "_find_powershell", lambda: "fixture-powershell")
    monkeypatch.setattr(
        lifecycle,
        "_harden_built_installer",
        lambda path, **_kwargs: {
            "id": lifecycle.INSTALLER_HARDENING_TRANSFORM,
            "artifact": path.name,
            "changes": ["fixture"],
        },
    )

    lifecycle._build_installers(root, revision, output, "v1.2.3")

    commit_env = next(env for argv, env in invocations if "commit" in argv)
    builder_env = next(
        env for argv, env in invocations if argv[0] == "fixture-powershell"
    )
    assert commit_env is not None
    assert builder_env is not None
    assert commit_env["GIT_AUTHOR_DATE"] == f"@{source_epoch} +0000"
    assert commit_env["GIT_COMMITTER_DATE"] == f"@{source_epoch} +0000"
    assert builder_env["SOURCE_DATE_EPOCH"] == str(source_epoch)
    assert builder_env["TZ"] == "UTC"


def test_third_review_transform_provenance_names_reparse_containment(
    tmp_path: Path,
) -> None:
    _tar_payload, zip_payload, _files = one_click_payloads()
    windows = put(
        tmp_path,
        "installer.cmd",
        b"""@echo off
#==PSPAYLOAD==#
$ErrorActionPreference = "Stop"
if ($sel.Name -eq "full") { Remove-Item (Join-Path $dest "serving\\models.profile") -ErrorAction SilentlyContinue }
else { Set-Content -Path (Join-Path $dest "serving\\models.profile") -Value (($sel.Models -split ",") -join "`n") }
Set-Content -Path (Join-Path $dest "serving\\tiers.env") -Value @("OPUS_MODEL=$($sel.Opus)", "SONNET_MODEL=$($sel.Sonnet)", "HAIKU_MODEL=$($sel.Haiku)")
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "connectors\\ide\\setup-ide.ps1") install
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bootstrap\\download-models.ps1")
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "bin\\oracle.ps1") setup
#==B64PAYLOAD==#
UEFZTE9BRA==
""",
    )
    windows.write_text(
        windows.read_text(encoding="ascii").replace(
            "UEFZTE9BRA==",
            base64.b64encode(zip_payload).decode("ascii"),
        ),
        encoding="ascii",
    )

    transform = lifecycle._harden_built_installer(windows)

    assert "reject-reparse-config-paths" in transform["changes"]


@pytest.mark.parametrize(
    "relative",
    [
        "bootstrap/install.sh",
        "harness/ecc/install-ecc.sh",
        "harness/skill-packs/install-skill-packs.sh",
        "harness/skill-packs/install-skill-packs.ps1",
        "harness/agent-mcp/setup-agent-mcp.sh",
        "harness/agent-mcp/setup-agent-mcp.ps1",
        "harness/loop-engineering/install-loop-eng.sh",
        "harness/loop-engineering/install-loop-eng.ps1",
    ],
)
def test_third_review_source_callers_preflight_inside_explicit_trusted_root(
    relative: str,
) -> None:
    source = (REPO_ROOT / relative).read_text(encoding="utf-8")

    assert "--trusted-root" in source
    assert "preflight-source" in source
    assert source.index("preflight-source") < source.index("install-source")


def test_final_review_engine_sync_uses_declared_profile_tiers_and_active_models() -> (
    None
):
    profile = lifecycle.resolve_sync_profile(
        (
            "full | 448 | huge,other,fast,embed | other | huge | fast | ~700 GB\n"
            "mid | 64 | fast,embed | fast | fast | fast | ~40 GB\n"
        ),
        active_names={"huge", "other", "fast", "embed"},
        detected_names={"huge", "other", "fast", "embed", "inactive"},
    )

    assert profile["active"] == ("huge", "other", "fast", "embed")
    assert profile["tiers"] == {
        "OPUS_MODEL": "other",
        "SONNET_MODEL": "huge",
        "HAIKU_MODEL": "fast",
    }


def test_final_review_model_authority_binds_layer_metadata(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "models": {
            "chat": {
                "repository": "example/chat",
                "revision": "a" * 40,
                "include": "*.gguf",
                "layer_mib": [5, 6, 7],
                "kv_mib_per_token": 0.125,
                "files": [
                    {
                        "path": "chat.gguf",
                        "sha256": "b" * 64,
                        "size": 1024,
                    }
                ],
            }
        },
    }
    put(
        tmp_path,
        "serving/model-authorities.json",
        json.dumps(payload, sort_keys=True) + "\n",
    )

    authority = lifecycle._model_authorities(tmp_path)["chat"]

    assert authority["layer_mib"] == [5, 6, 7]
    assert authority["kv_mib_per_token"] == 0.125
    assert lifecycle._model_authority_digest(
        authority
    ) != lifecycle._model_authority_digest(
        {key: value for key, value in authority.items() if key != "layer_mib"}
    )
