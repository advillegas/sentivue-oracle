from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import verification.lifecycle as lifecycle
from verification.lifecycle import (
    ArtifactRecord,
    LifecycleError,
    atomic_write_text,
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
        assert not any(
            blocked in name for name in names for blocked in blocked_parts
        )
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zipped:
                assert (
                    zipped.read("sentivue-oracle/src/runtime source.py")
                    == b"VALUE = 'committed'\n"
                )
        else:
            with tarfile.open(archive, "r:gz") as tarred:
                stream = tarred.extractfile(
                    "sentivue-oracle/src/runtime source.py"
                )
                assert stream is not None
                assert stream.read() == b"VALUE = 'committed'\n"

    verify_release_bundle(output)


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


def test_release_preflight_failure_runs_no_mutating_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise LifecycleError("fixture preflight failed")

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
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


def test_operator_targets_preserve_safe_lifecycle_defaults() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'bootstrap/package.sh --version "$(VERSION)"' in makefile
    assert "$(CONFIRM_PURGE)" in makefile
    assert "--confirm-purge" in makefile
    assert "release.ps1 -Version vX.Y.Z -Publish" in readme


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

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[:3] == ["git", "show-ref", "--verify"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)

    with pytest.raises(LifecycleError, match="already exists"):
        publish_release(tmp_path, "v1.2.3", output)

    flattened = [" ".join(command) for command in commands]
    assert not any(
        token in command
        for command in flattened
        for token in (" push ", " release create ", " release delete ", "--force")
    )


def test_release_publication_is_create_only_after_verified_preflight(
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

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[:3] in (
            ["git", "show-ref", "--verify"],
            ["git", "ls-remote", "--exit-code"],
            ["gh", "release", "view"],
        ):
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)

    publish_release(tmp_path, "v9.9.9", output)

    rendered = [" ".join(command) for command in commands]
    assert any(command.startswith("git tag v9.9.9 ") for command in rendered)
    assert any(command == "git push origin refs/tags/v9.9.9" for command in rendered)
    assert any(command.startswith("gh release create v9.9.9 ") for command in rendered)
    assert not any("--force" in command or "delete" in command for command in rendered)


def dependency_fixture(root: Path, dynamic: bool = True) -> None:
    continue_version = "dynamic" if dynamic else "1.2.3"
    put(
        root,
        "VERSIONS.lock",
        "LLAMA_SWAP_VERSION=v236\n"
        f"CONTINUE_VSIX_VERSION={continue_version}\n"
        "KILO_VSIX_VERSION=7.4.5\n",
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
                        "version_key": "LLAMA_SWAP_VERSION",
                        "allow_dynamic": False,
                    },
                    {
                        "id": "continue-vsix",
                        "version_key": "CONTINUE_VSIX_VERSION",
                        "allow_dynamic": True,
                    },
                    {
                        "id": "kilo-vsix",
                        "version_key": "KILO_VSIX_VERSION",
                        "allow_dynamic": False,
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
    assert any("continue-vsix" in error and "resolved artifact" in error for error in errors)
    assert any("model:chat" in error and "resolved artifact" in error for error in errors)

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
            requested_version="dynamic" if artifact_id in {"continue-vsix", "model:chat"} else resolved,
            resolved_version=resolved,
        )
    put(tmp_path, "models/chat/model.gguf", b"GGUF model")
    record_model_snapshot(
        tmp_path,
        cache,
        model_name="chat",
        repository="example/chat",
        requested_revision="dynamic",
        resolved_revision="f" * 40,
    )

    assert validate_dependency_inputs(
        tmp_path,
        artifact_manifest=cache / "manifest.json",
        cache_root=cache,
        reproducible=True,
    ) == []


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
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(
        root,
        "serving/models.manifest",
        "# name | repo | include | slot | ctx | flags | revision\n"
        "chat-model | example/chat | model.gguf | fast | 32768 | --temp 0.7 | "
        + ("a" * 40)
        + "\n"
        "embed-model | example/embed | embed.gguf | embed | 8192 | | "
        + ("b" * 40)
        + "\n",
    )
    put(root, "models/chat-model/model.gguf", b"GGUF chat")
    put(root, "models/embed-model/embed.gguf", b"GGUF embed")
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
    assert (home / ".continue/config.oracle.yaml").is_file()
    assert (home / ".config/kilo/kilo.oracle.jsonc").is_file()
    assert (root / "state/generated/claude-code/settings.json").is_file()
    assert (root / "state/generated/opencode/opencode.json").is_file()
    assert (root / "serving/tiers.env").read_text(encoding="utf-8") == (
        "OPUS_MODEL=chat-model\n"
        "SONNET_MODEL=chat-model\n"
        "HAIKU_MODEL=chat-model\n"
    )
    for path, digest in template_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    for path in written:
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    state = json.loads(
        (root / ".install-state/state.json").read_text(encoding="utf-8")
    )
    owned = {(item["scope"], item["path"]) for item in state["owned_paths"]}
    assert ("home", ".continue/config.oracle.yaml") in owned
    assert ("home", ".config/kilo/kilo.oracle.jsonc") in owned
    assert len(owned) == len(state["owned_paths"])


def test_generated_configs_refuse_symbolic_link_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    put(root, "VERSIONS.lock", "TOOL_VERSION=1.0.0\n")
    put(
        root,
        "serving/models.manifest",
        "chat | example/chat | model.gguf | fast | 32768 | | " + "a" * 40 + "\n",
    )
    put(root, "models/chat/model.gguf", b"GGUF")
    original_is_symlink = Path.is_symlink
    linked_parent = home / ".continue"

    def fake_is_symlink(self: Path) -> bool:
        return self == linked_parent or original_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(LifecycleError, match="symbolic-link parent"):
        sync_model_configs(root, home)
    assert not (home / ".continue/config.yaml").exists()


def test_lifecycle_scripts_have_cross_platform_twins_and_protected_builder_is_unchanged() -> None:
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


def test_install_consumers_use_export_cache_instead_of_dynamic_network_resolution() -> None:
    windows_serving = (
        REPO_ROOT / "serving/serve-windows.ps1"
    ).read_text(encoding="utf-8")
    mac_install = (REPO_ROOT / "bootstrap/install.sh").read_text(encoding="utf-8")
    ide_powershell = (
        REPO_ROOT / "connectors/ide/setup-ide.ps1"
    ).read_text(encoding="utf-8")
    ide_bash = (REPO_ROOT / "connectors/ide/setup-ide.sh").read_text(
        encoding="utf-8"
    )
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


def test_doctors_report_dependency_cache_and_install_state_health() -> None:
    for relative in ("bootstrap/doctor.ps1", "bootstrap/doctor.sh"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "validate-dependencies" in source
        assert "dependency-cache" in source
        assert ".install-state" in source


def test_platform_generated_config_writers_use_same_directory_atomic_replacement() -> None:
    render = (REPO_ROOT / "bootstrap/render-config.sh").read_text(encoding="utf-8")
    service = (REPO_ROOT / "serving/service.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "serving/serve-windows.ps1").read_text(
        encoding="utf-8"
    )

    assert 'mktemp "${OUT}.tmp.' in render
    assert 'mktemp "${PLIST}.tmp.' in service
    assert "Write-Utf8NoBomAtomic $Rendered" in windows


def test_model_downloaders_resolve_revision_and_record_local_hashes() -> None:
    bash_source = (REPO_ROOT / "bootstrap/download-models.sh").read_text(
        encoding="utf-8"
    )
    powershell_source = (
        REPO_ROOT / "bootstrap/download-models.ps1"
    ).read_text(encoding="utf-8")

    assert "resolved_revision" in bash_source
    assert '--revision "$resolved_revision"' in bash_source
    assert "record-model" in bash_source
    assert "Revision = $f[6].Trim()" in powershell_source
    assert "record-model" in powershell_source
    assert "tree/main" not in powershell_source
    assert "resolve/main" not in powershell_source
    render_source = (REPO_ROOT / "bootstrap/render-config.sh").read_text(
        encoding="utf-8"
    )
    assert "read -r name repo include slot ctx flags revision" in render_source
