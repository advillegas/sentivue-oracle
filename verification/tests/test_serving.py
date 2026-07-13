from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import verification.serving as serving
from verification.serving import (
    FAIL,
    PASS,
    PROVISIONAL,
    SKIP,
    Backend,
    HttpResponse,
    PidRecord,
    ResourceSnapshot,
    ServingError,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
GIB = 1024


def put(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def profiles_text() -> str:
    return (
        "# name | minimum GiB | models | opus | sonnet | haiku | download\n"
        "full | 448 | huge,fast,embed | huge | huge | fast | ~700 GB\n"
        "coder | 320 | huge,fast,embed | huge | huge | fast | ~315 GB\n"
        "mid | 64 | fast,embed | fast | fast | fast | ~40 GB\n"
        "lite | 24 | small,embed | small | small | small | ~24 GB\n"
        "micro | 8 | tiny,embed | tiny | tiny | tiny | ~10 GB\n"
    )


def manifest_text() -> str:
    revision = "a" * 40
    return (
        "# name | repo | include | slot | context | flags | revision\n"
        f"huge | example/huge | *.gguf | big | 131072 | --temp 0.7 | {revision}\n"
        f"fast | example/fast | *.gguf | fast | 65536 | --temp 0.7 | {revision}\n"
        f"small | example/small | *.gguf | fast | 65536 | --temp 0.7 | {revision}\n"
        f"tiny | example/tiny | *.gguf | fast | 16384 | --temp 0.7 | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n"
    )


@pytest.mark.parametrize(
    ("total", "available", "backend", "accelerator", "unified", "expected"),
    [
        (16, 15, Backend.CPU, 0, False, "micro"),
        (32, 30, Backend.CPU, 0, False, "lite"),
        (64, 60, Backend.CUDA, 24, False, "mid"),
        (128, 120, Backend.VULKAN, 0, False, "mid"),
        (128, 120, Backend.METAL, 96, True, "mid"),
        (512, 500, Backend.METAL, 448, True, "full"),
    ],
)
def test_resource_profile_matrix_uses_available_memory_and_explicit_headroom(
    total: int,
    available: int,
    backend: Backend,
    accelerator: int,
    unified: bool,
    expected: str,
) -> None:
    profiles = serving.parse_profiles(profiles_text())
    snapshot = ResourceSnapshot(
        system_total_mib=total * GIB,
        system_available_mib=available * GIB,
        backend=backend,
        accelerator_total_mib=accelerator * GIB,
        accelerator_available_mib=accelerator * GIB,
        accelerator_shared=unified,
        capability_source="fixture",
    )

    selected = serving.select_profile(profiles, snapshot)

    assert selected.name == expected
    assert snapshot.os_reserve_mib > 0
    assert snapshot.runtime_reserve_mib > 0


def test_unified_memory_is_never_double_counted() -> None:
    snapshot = ResourceSnapshot(
        system_total_mib=64 * GIB,
        system_available_mib=60 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=48 * GIB,
        accelerator_available_mib=48 * GIB,
        accelerator_shared=True,
        capability_source="fixture-metal",
    )

    assert serving.usable_capacity_mib(snapshot) == (
        snapshot.system_available_mib
        - snapshot.os_reserve_mib
        - snapshot.runtime_reserve_mib
    )


def test_discrete_accelerator_uses_only_free_memory_after_reserve() -> None:
    snapshot = ResourceSnapshot(
        system_total_mib=64 * GIB,
        system_available_mib=48 * GIB,
        backend=Backend.CUDA,
        accelerator_total_mib=24 * GIB,
        accelerator_available_mib=20 * GIB,
        accelerator_shared=False,
        capability_source="nvidia-smi",
    )

    assert serving.usable_capacity_mib(snapshot) == (
        snapshot.system_available_mib
        - snapshot.os_reserve_mib
        - snapshot.runtime_reserve_mib
        + snapshot.accelerator_available_mib
        - snapshot.accelerator_reserve_mib
    )


def test_cpu_backend_never_counts_discrete_accelerator_memory() -> None:
    snapshot = ResourceSnapshot(
        system_total_mib=32 * GIB,
        system_available_mib=20 * GIB,
        backend=Backend.CPU,
        accelerator_total_mib=80 * GIB,
        accelerator_available_mib=72 * GIB,
        accelerator_shared=False,
        capability_source="explicit CPU fallback",
    )

    assert serving.usable_capacity_mib(snapshot) == (
        snapshot.system_available_mib
        - snapshot.os_reserve_mib
        - snapshot.runtime_reserve_mib
    )


def test_nvidia_smi_parser_preserves_exact_mib_and_multiple_devices() -> None:
    devices = serving.parse_nvidia_smi(
        "24564, 23117, NVIDIA RTX fixture\n"
        "12282 MiB, 10001 MiB, NVIDIA secondary\n"
    )

    assert [(item.total_mib, item.free_mib) for item in devices] == [
        (24564, 23117),
        (12282, 10001),
    ]
    assert serving.aggregate_nvidia_memory(devices) == (36846, 33118)


@pytest.mark.parametrize("bad", ["", "4096", "four, 3000, gpu", "4096, 5000, gpu"])
def test_nvidia_smi_parser_fails_closed_on_unusable_output(bad: str) -> None:
    with pytest.raises(ServingError):
        serving.parse_nvidia_smi(bad)


def test_windows_resource_detection_never_uses_truncated_adapter_ram() -> None:
    snapshot = serving.windows_resource_snapshot(
        total_mib=32 * GIB,
        available_mib=28 * GIB,
        nvidia_smi_output=None,
        vulkan_available=True,
        adapter_ram_bytes=4 * 1024**3,
    )

    assert snapshot.backend == Backend.VULKAN
    assert snapshot.accelerator_total_mib == 0
    assert snapshot.accelerator_available_mib == 0
    assert "AdapterRAM ignored" in snapshot.capability_source


def test_windows_nvidia_smi_data_selects_cuda_explicitly() -> None:
    snapshot = serving.windows_resource_snapshot(
        total_mib=64 * GIB,
        available_mib=56 * GIB,
        nvidia_smi_output="24564, 22001, RTX fixture\n",
        vulkan_available=True,
        adapter_ram_bytes=4 * 1024**3,
    )

    assert snapshot.backend == Backend.CUDA
    assert snapshot.accelerator_total_mib == 24564
    assert snapshot.accelerator_available_mib == 22001
    assert snapshot.capability_source == "nvidia-smi exact MiB"


@pytest.mark.parametrize("backend", list(Backend))
def test_backend_selection_is_explicit_and_observable(backend: Backend) -> None:
    selected = serving.select_backend(
        backend.value,
        available={Backend.CPU, backend},
    )
    assert selected == backend


def test_backend_request_fails_when_capability_is_unavailable() -> None:
    with pytest.raises(ServingError, match="unavailable"):
        serving.select_backend("cuda", available={Backend.CPU, Backend.VULKAN})


def test_loaded_backend_is_separate_from_inferred_capability() -> None:
    evidence = serving.backend_evidence(Backend.CUDA, "nvidia-smi exact MiB")
    assert evidence["selected_backend"] == "cuda"
    assert evidence["capability_source"] == "nvidia-smi exact MiB"
    assert evidence["loaded_backend"] is None
    assert evidence["offloaded_layers"] is None

    loaded = serving.with_loaded_backend(evidence, "CUDA0", 61)
    assert loaded["loaded_backend"] == "cuda"
    assert loaded["offloaded_layers"] == 61


def test_profile_manifest_tiers_and_active_profile_are_parsed_consistently() -> None:
    profiles = serving.parse_profiles(profiles_text())
    models = serving.parse_manifest(manifest_text())
    active = serving.parse_active_models("fast\nembed\n")
    tiers = serving.parse_tiers(
        "OPUS_MODEL=fast\nSONNET_MODEL=fast\nHAIKU_MODEL=fast\n"
    )

    resolved = serving.resolve_active_profile(profiles, models, active, tiers)

    assert resolved.name == "mid"
    assert resolved.models == ("fast", "embed")


@pytest.mark.parametrize(
    ("kind", "content", "message"),
    [
        (
            "profiles",
            "micro | 8 | missing | missing | missing | missing | ~1 GB\n",
            "unknown model",
        ),
        (
            "manifest",
            "chat | repo | *.gguf | mystery | 32768 | | dynamic\n",
            "slot",
        ),
        (
            "tiers",
            "OPUS_MODEL=chat\nOPUS_MODEL=other\n",
            "duplicate",
        ),
        ("active", "chat\nchat\n", "duplicate"),
    ],
)
def test_serving_parsers_fail_closed(
    kind: str, content: str, message: str
) -> None:
    if kind == "profiles":
        with pytest.raises(ServingError, match=message):
            serving.validate_profile_references(
                serving.parse_profiles(content),
                serving.parse_manifest(manifest_text()),
            )
    else:
        parser = {
            "manifest": serving.parse_manifest,
            "tiers": serving.parse_tiers,
            "active": serving.parse_active_models,
        }[kind]
        with pytest.raises(ServingError, match=message):
            parser(content)


@pytest.mark.parametrize(
    "flag",
    [
        "--host=0.0.0.0",
        "--port=8080",
        "--ctx-size=999999",
        "--parallel=99",
        "--n-gpu-layers=999",
        "--model=outside.gguf",
    ],
)
def test_manifest_flags_cannot_override_managed_runtime_options(flag: str) -> None:
    with pytest.raises(ServingError, match="managed runtime"):
        serving.parse_manifest(
            "chat | example/chat | *.gguf | fast | 32768 | "
            f"{flag} | {'a' * 40}\n"
        )


def test_tier_or_active_profile_mismatch_does_not_collapse_silently() -> None:
    profiles = serving.parse_profiles(profiles_text())
    models = serving.parse_manifest(manifest_text())

    with pytest.raises(ServingError, match="tier"):
        serving.resolve_active_profile(
            profiles,
            models,
            serving.parse_active_models("fast\nembed\n"),
            serving.parse_tiers(
                "OPUS_MODEL=tiny\nSONNET_MODEL=fast\nHAIKU_MODEL=fast\n"
            ),
        )


def policy_bound_fixture(root: Path) -> tuple[dict[str, serving.ModelSpec], tuple[str, ...]]:
    revision = "a" * 40
    manifest = serving.parse_manifest(
        f"chat | example/chat | *.gguf | fast | 65536 | --temp 0.7 | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n"
    )
    authorities: dict[str, object] = {"schema_version": 1, "models": {}}
    for name, body in (("chat", b"GGUF chat fixture"), ("embed", b"GGUF embed fixture")):
        relative = f"{name}.gguf"
        put(root, f"models/{name}/{relative}", body)
        authorities["models"][name] = {  # type: ignore[index]
            "repository": f"example/{name}",
            "revision": revision,
            "include": "*.gguf",
            "files": [
                {
                    "path": relative,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                }
            ],
        }
    put(
        root,
        "serving/model-authorities.json",
        json.dumps(authorities, sort_keys=True) + "\n",
    )
    return manifest, ("chat", "embed")


def test_selected_models_require_complete_policy_bound_snapshots(tmp_path: Path) -> None:
    models, selected = policy_bound_fixture(tmp_path)

    snapshots = serving.validate_policy_bound_models(tmp_path, models, selected)

    assert set(snapshots) == {"chat", "embed"}
    assert snapshots["chat"].size_bytes == len(b"GGUF chat fixture")

    (tmp_path / "models/chat/chat.gguf").write_bytes(b"tampered")
    with pytest.raises(ServingError, match="digest|size"):
        serving.validate_policy_bound_models(tmp_path, models, selected)


@pytest.mark.parametrize("failure", ["missing-authority", "unsupported-model", "missing-file"])
def test_missing_untrusted_or_unsupported_models_fail_reproducible_serving(
    tmp_path: Path, failure: str
) -> None:
    models, selected = policy_bound_fixture(tmp_path)
    if failure == "missing-authority":
        put(
            tmp_path,
            "serving/model-authorities.json",
            '{"schema_version": 1, "models": {}}\n',
        )
    elif failure == "unsupported-model":
        selected = ("not-declared",)
    else:
        (tmp_path / "models/chat/chat.gguf").unlink()

    with pytest.raises(ServingError):
        serving.validate_policy_bound_models(tmp_path, models, selected)


def test_context_plan_accounts_for_slots_overhead_kv_model_and_runtime_memory() -> None:
    plan = serving.plan_context(
        model_name="chat",
        nominal_context_tokens=65536,
        requested_parallel=2,
        model_memory_mib=20 * GIB,
        usable_memory_mib=36 * GIB,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.0625,
    )

    assert plan.parallel_slots == 2
    assert plan.slot_context_tokens <= 65536 // 2
    assert plan.advertised_context_tokens == (
        plan.slot_context_tokens
        - plan.prompt_tool_overhead_tokens
        - plan.output_reserve_tokens
    )
    assert plan.peak_memory_mib <= plan.usable_memory_mib
    assert plan.advertised_context_tokens > 0


def test_unsafe_parallelism_is_reduced_before_config_render() -> None:
    plan = serving.plan_context(
        model_name="chat",
        nominal_context_tokens=65536,
        requested_parallel=4,
        model_memory_mib=28 * GIB,
        usable_memory_mib=35 * GIB,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.125,
    )

    assert plan.parallel_slots < 4
    assert plan.peak_memory_mib <= plan.usable_memory_mib


def test_impossible_context_is_rejected_instead_of_advertised() -> None:
    with pytest.raises(ServingError, match="context"):
        serving.plan_context(
            model_name="chat",
            nominal_context_tokens=8192,
            requested_parallel=2,
            model_memory_mib=31 * GIB,
            usable_memory_mib=32 * GIB,
            prompt_tool_overhead_tokens=4096,
            output_reserve_tokens=4096,
            kv_mib_per_token=0.125,
        )


def test_request_estimator_includes_messages_tools_and_output() -> None:
    estimate = serving.estimate_request_tokens(
        {
            "messages": [
                {"role": "system", "content": "policy " * 100},
                {"role": "user", "content": "work " * 1000},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {"type": "object", "properties": {"path": {}}},
                    },
                }
            ],
            "max_tokens": 2048,
        }
    )

    assert estimate.prompt_tokens > 1100
    assert estimate.tool_schema_tokens > 0
    assert estimate.output_tokens == 2048
    embedding = serving.estimate_request_tokens({"input": "vector " * 9000})
    assert embedding.prompt_tokens >= 9000


def test_request_estimator_counts_top_level_system_and_output_schema() -> None:
    estimate = serving.estimate_request_tokens(
        {
            "system": "policy " * 500,
            "messages": [{"role": "user", "content": "work"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "schema": {
                        "type": "object",
                        "description": "schema " * 1200,
                    },
                },
            },
            "max_tokens": 128,
        }
    )
    assert estimate.prompt_tokens >= 500
    assert estimate.tool_schema_tokens >= 1200
    assert estimate.output_tokens == 128

    for invalid in (True, 1.5, "128"):
        with pytest.raises(ServingError, match="max_tokens"):
            serving.estimate_request_tokens({"max_tokens": invalid})


def test_oversize_and_contention_reject_before_upstream_invocation() -> None:
    plan = serving.plan_context(
        model_name="chat",
        nominal_context_tokens=65536,
        requested_parallel=1,
        model_memory_mib=20 * GIB,
        usable_memory_mib=40 * GIB,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.0625,
    )
    invoked: list[object] = []

    with pytest.raises(ServingError, match="context"):
        serving.dispatch_admitted(
            plan,
            {"messages": [{"role": "user", "content": "x " * 70000}]},
            active_requests=0,
            invoke=lambda payload: invoked.append(payload),
        )
    with pytest.raises(ServingError, match="contention"):
        serving.dispatch_admitted(
            plan,
            {"messages": [{"role": "user", "content": "small"}]},
            active_requests=plan.parallel_slots,
            invoke=lambda payload: invoked.append(payload),
        )
    assert invoked == []


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("::", False),
        ("*", False),
        ("192.168.1.2", False),
    ],
)
def test_runtime_bind_must_be_loopback(value: str, valid: bool) -> None:
    if valid:
        assert serving.require_loopback(value) == value
    else:
        with pytest.raises(ServingError, match="loopback"):
            serving.require_loopback(value)


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_generated_runtime_config_is_atomic_parseable_and_path_safe(
    tmp_path: Path, platform: str
) -> None:
    root = tmp_path / "repository with spaces & (shell)"
    root.mkdir()
    server = put(root, "tools & runtime/llama server.exe", b"fixture")
    chat = put(root, "models/chat & tools/chat model.gguf", b"fixture")
    plan = serving.plan_context(
        model_name="chat",
        nominal_context_tokens=32768,
        requested_parallel=1,
        model_memory_mib=1,
        usable_memory_mib=16 * GIB,
        prompt_tool_overhead_tokens=1024,
        output_reserve_tokens=1024,
        kv_mib_per_token=0.001,
    )
    model = serving.ModelSpec(
        name="chat",
        repository="example/chat",
        include="*.gguf",
        slot="fast",
        nominal_context=32768,
        flags=("--temp", "0.7"),
        revision="a" * 40,
    )
    output = root / "state/generated/serving/llama-swap.yaml"

    rendered = serving.render_runtime_config(
        output=output,
        server_path=server,
        model_paths={"chat": (chat,)},
        models={"chat": model},
        contexts={"chat": plan},
        backend=Backend.CPU,
        platform=platform,
    )

    assert rendered.path == output
    assert output.read_bytes()[:3] != b"\xef\xbb\xbf"
    assert not list(output.parent.glob("*.tmp"))
    parsed = serving.parse_runtime_config(output.read_text(encoding="utf-8"))
    command = parsed["models"]["chat"]["cmd"]
    argv = (
        shlex.split(command)
        if platform == "posix"
        else serving.command_line_to_argv_windows(command)
    )
    assert argv[0] == str(server)
    assert argv[argv.index("-m") + 1] == str(chat)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--n-gpu-layers") + 1] == "0"
    assert parsed["models"]["chat"]["advertised_context"] == (
        plan.advertised_context_tokens
    )


def test_generated_config_rejects_non_loopback_and_multi_shard_ambiguity(
    tmp_path: Path,
) -> None:
    with pytest.raises(ServingError, match="loopback"):
        serving.render_runtime_config(
            output=tmp_path / "state/generated/serving/config.yaml",
            server_path=tmp_path / "server",
            model_paths={"chat": (tmp_path / "one.gguf", tmp_path / "two.gguf")},
            models={},
            contexts={},
            backend=Backend.CPU,
            platform="posix",
            host="0.0.0.0",
        )


def test_repository_runtime_plan_is_policy_bound_and_generated_under_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository with spaces"
    root.mkdir()
    revision = "a" * 40
    put(
        root,
        "serving/profiles.conf",
        "micro | 8 | chat,embed | chat | chat | chat | ~1 GB\n",
    )
    put(
        root,
        "serving/models.manifest",
        f"chat | example/chat | *.gguf | fast | 32768 | --temp 0.7 | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n",
    )
    put(root, "serving/models.profile", "chat\nembed\n")
    put(
        root,
        "serving/tiers.env",
        "OPUS_MODEL=chat\nSONNET_MODEL=chat\nHAIKU_MODEL=chat\n",
    )
    policy_bound_fixture(root)
    server = put(root, "tools/llama server", b"fixture executable")
    snapshot = ResourceSnapshot(
        system_total_mib=32 * GIB,
        system_available_mib=30 * GIB,
        backend=Backend.CPU,
        capability_source="fixture",
    )

    plan = serving.prepare_runtime(
        root=root,
        server_path=server,
        platform="posix",
        resources=snapshot,
        requested_backend="cpu",
    )

    assert plan.profile.name == "micro"
    assert plan.rendered.path == (
        root / "state/generated/serving/llama-swap.yaml"
    )
    assert plan.rendered.metadata_path == (
        root / "state/generated/serving/admission.json"
    )
    assert not (root / "serving/llama-swap.rendered.yaml").exists()
    metadata = json.loads(plan.rendered.metadata_path.read_text(encoding="utf-8"))
    assert metadata["profile"] == "micro"
    assert metadata["tiers"]["HAIKU_MODEL"] == "chat"
    assert metadata["models"]["chat"]["slot"] == "fast"
    assert metadata["models"]["embed"]["slot"] == "embed"
    assert metadata["models"]["chat"]["advertised_context"] < 32768
    assert metadata["config_sha256"] == hashlib.sha256(
        plan.rendered.path.read_bytes()
    ).hexdigest()
    assert plan.rendered.path.read_bytes()[:3] != b"\xef\xbb\xbf"


def test_active_profile_must_still_fit_current_resource_capacity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "undersized host"
    root.mkdir()
    revision = "a" * 40
    put(
        root,
        "serving/profiles.conf",
        "mid | 64 | chat,embed | chat | chat | chat | ~1 GB\n",
    )
    put(
        root,
        "serving/models.manifest",
        f"chat | example/chat | *.gguf | fast | 32768 | | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n",
    )
    put(root, "serving/models.profile", "chat\nembed\n")
    put(
        root,
        "serving/tiers.env",
        "OPUS_MODEL=chat\nSONNET_MODEL=chat\nHAIKU_MODEL=chat\n",
    )
    policy_bound_fixture(root)
    server = put(root, "tools/llama-server", b"fixture")
    resources = ResourceSnapshot(
        system_total_mib=32 * GIB,
        system_available_mib=30 * GIB,
        backend=Backend.CPU,
        capability_source="fixture",
    )

    with pytest.raises(ServingError, match="requires 64.0 GiB"):
        serving.prepare_runtime(
            root=root,
            server_path=server,
            platform="posix",
            resources=resources,
            requested_backend="cpu",
        )


def test_request_estimator_counts_tool_call_history_inside_messages() -> None:
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "fixture",
                            "arguments": "x" * 40000,
                        }
                    }
                ],
            }
        ],
        "max_tokens": 64,
    }

    estimate = serving.estimate_request_tokens(payload)

    assert estimate.prompt_tokens >= 10000


def test_admission_controller_rejects_contention_and_oversize_before_forwarding() -> None:
    plan = production_plan()
    controller = serving.AdmissionController({"chat": plan})
    first = controller.try_begin(
        "chat",
        {"messages": [{"role": "user", "content": "safe request"}]},
    )
    forwarded: list[str] = []

    with pytest.raises(ServingError, match="contention"):
        controller.try_begin(
            "chat",
            {"messages": [{"role": "user", "content": "second request"}]},
        )
    with pytest.raises(ServingError, match="context"):
        controller.try_begin(
            "chat",
            {"messages": [{"role": "user", "content": "x " * 70000}]},
        )
    assert forwarded == []

    first.close()
    second = controller.try_begin(
        "chat",
        {"messages": [{"role": "user", "content": "safe again"}]},
    )
    second.close()


def test_admission_controller_rejects_cross_model_exclusive_group_contention() -> None:
    plans = {
        name: serving.plan_context(
            model_name=name,
            nominal_context_tokens=65536,
            requested_parallel=1,
            model_memory_mib=8 * GIB,
            usable_memory_mib=32 * GIB,
            prompt_tool_overhead_tokens=4096,
            output_reserve_tokens=4096,
            kv_mib_per_token=0.0625,
        )
        for name in ("big-a", "big-b")
    }
    controller = serving.AdmissionController(
        plans, exclusive_groups=(frozenset(plans),)
    )
    first = controller.try_begin(
        "big-a", {"messages": [{"role": "user", "content": "work"}]}
    )
    with pytest.raises(ServingError, match="contention"):
        controller.try_begin(
            "big-b", {"messages": [{"role": "user", "content": "work"}]}
        )
    first.close()
    second = controller.try_begin(
        "big-b", {"messages": [{"role": "user", "content": "work"}]}
    )
    second.close()


def test_loopback_gateway_rejects_oversize_before_upstream_http_call() -> None:
    upstream_calls: list[bytes] = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            upstream_calls.append(self.rfile.read(length))
            body = b'{"choices":[{"message":{"content":"OK"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    gateway = serving.create_admission_server(
        host="127.0.0.1",
        port=0,
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        contexts={"chat": production_plan()},
        evidence={"profile": "fixture", "api_key": "must-redact"},
    )
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    endpoint = f"http://127.0.0.1:{gateway.server_port}/v1/chat/completions"

    try:
        safe = json.dumps(
            {
                "model": "chat",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "safe"}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=safe, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert len(upstream_calls) == 1

        oversize = json.dumps(
            {
                "model": "chat",
                "messages": [{"role": "user", "content": "x " * 70000}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=oversize, headers={"Content-Type": "application/json"}
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request, timeout=5)
        assert rejected.value.code == 413
        assert rejected.value.headers["X-Oracle-Admission"] == "rejected"
        assert len(upstream_calls) == 1

        with urllib.request.urlopen(
            f"http://127.0.0.1:{gateway.server_port}/oracle/capabilities",
            timeout=5,
        ) as response:
            capabilities = response.read().decode("utf-8")
        assert "must-redact" not in capabilities
        assert "[REDACTED]" in capabilities
    finally:
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()


def test_pid_identity_rejects_stale_reused_and_unowned_processes() -> None:
    record = PidRecord(
        pid=42,
        executable="/oracle/llama-swap",
        started_at=100.0,
        command_digest="a" * 64,
    )
    assert serving.process_matches_pid_record(
        record,
        executable="/oracle/llama-swap",
        started_at=100.0,
        command_digest="a" * 64,
    )
    assert not serving.process_matches_pid_record(
        record,
        executable="/other/process",
        started_at=100.0,
        command_digest="a" * 64,
    )
    assert not serving.process_matches_pid_record(
        record,
        executable="/oracle/llama-swap",
        started_at=101.0,
        command_digest="a" * 64,
    )


def test_stop_refuses_pid_reuse_before_kill() -> None:
    killed: list[int] = []
    record = PidRecord(42, "/oracle/runner", 100.0, "a" * 64)
    with pytest.raises(ServingError, match="refusing"):
        serving.stop_recorded_process(
            record,
            inspect=lambda _pid: ("/other/process", 100.0, "a" * 64),
            terminate=lambda pid: killed.append(pid),
        )
    assert killed == []


def production_plan() -> serving.ContextPlan:
    return serving.plan_context(
        model_name="chat",
        nominal_context_tokens=65536,
        requested_parallel=1,
        model_memory_mib=10 * GIB,
        usable_memory_mib=48 * GIB,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.0625,
    )


def admission_fields(
    plan: serving.ContextPlan, *, slot: str = "fast"
) -> dict[str, object]:
    return {
        "slot": slot,
        "nominal_context": plan.nominal_context_tokens,
        "server_context": plan.slot_context_tokens * plan.parallel_slots,
        "parallel_slots": plan.parallel_slots,
        "slot_context": plan.slot_context_tokens,
        "advertised_context": plan.advertised_context_tokens,
        "prompt_tool_overhead": plan.prompt_tool_overhead_tokens,
        "output_reserve": plan.output_reserve_tokens,
        "model_memory_mib": plan.model_memory_mib,
        "kv_mib_per_token": plan.kv_mib_per_token,
        "peak_memory_mib": plan.peak_memory_mib,
        "usable_memory_mib": plan.usable_memory_mib,
    }


def test_offline_verifier_uses_production_shaped_api_payloads_and_all_protocols() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def transport(
        method: str, path: str, payload: dict[str, object] | None
    ) -> HttpResponse:
        requests.append((method, path, payload))
        if path == "/health":
            return HttpResponse(200, {"status": "ok"}, {}, 5)
        if path == "/v1/models":
            return HttpResponse(200, {"data": [{"id": "chat"}, {"id": "embed"}]}, {}, 5)
        if path == "/running":
            return HttpResponse(
                200,
                {
                    "running": [
                        {
                            "model": "chat",
                            "state": "ready",
                            "backend": "cuda",
                            "offloaded_layers": 61,
                        }
                    ]
                },
                {},
                5,
            )
        if path == "/v1/embeddings":
            return HttpResponse(200, {"data": [{"embedding": [0.0] * 128}]}, {}, 8)
        if path == "/v1/messages":
            return HttpResponse(200, {"content": [{"type": "text", "text": "ORACLE-OK"}]}, {}, 9)
        if path == "/v1/chat/completions":
            if payload:
                messages = payload.get("messages")
                if (
                    isinstance(messages, list)
                    and messages
                    and isinstance(messages[0], dict)
                    and str(messages[0].get("content", "")).startswith(
                        "oversize-boundary "
                    )
                ):
                    return HttpResponse(
                        413,
                        {"error": "rejected before upstream"},
                        {"X-Oracle-Admission": "rejected"},
                        1,
                    )
            if payload and payload.get("response_format"):
                return HttpResponse(
                    200,
                    {"choices": [{"message": {"content": '{"ok":true}'}}]},
                    {},
                    9,
                )
            if payload and payload.get("tools"):
                return HttpResponse(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {"function": {"name": "oracle_probe", "arguments": "{}"}}
                                    ]
                                }
                            }
                        ]
                    },
                    {},
                    9,
                )
            return HttpResponse(
                200,
                {"choices": [{"message": {"content": "ORACLE-OK"}}]},
                {},
                9,
            )
        raise AssertionError(path)

    results = serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        engine_runner=lambda name: (0, f"{name}: ENGINE-OK"),
    )

    assert {result.name for result in results} >= {
        "health",
        "model_identity",
        "loopback_binding",
        "openai_chat",
        "openai_tools",
        "openai_json",
        "anthropic_messages",
        "embeddings",
        "context_boundary",
        "context_oversize",
        "cold_warm_state",
        "loaded_backend",
        "headless_claude",
        "headless_opencode",
    }
    assert all(result.status == PASS for result in results)
    loaded = next(result for result in results if result.name == "loaded_backend")
    assert loaded.evidence["loaded_backend"] == "cuda"
    assert loaded.evidence["offloaded_layers"] == 61
    production_chat = next(
        payload
        for _, path, payload in requests
        if path == "/v1/chat/completions"
        and payload
        and not payload.get("tools")
        and not payload.get("response_format")
    )
    estimate = serving.estimate_request_tokens(production_chat)
    assert estimate.prompt_tokens >= 25000
    plan = production_plan()
    assert (
        estimate.total_tokens
        + plan.prompt_tool_overhead_tokens
        + plan.output_reserve_tokens
        <= plan.slot_context_tokens
    )
    boundary_chat = next(
        payload
        for _, path, payload in requests
        if path == "/v1/chat/completions"
        and payload
        and isinstance(payload.get("messages"), list)
        and str(payload["messages"][0].get("content", "")).startswith("boundary ")
    )
    boundary_estimate = serving.estimate_request_tokens(boundary_chat)
    boundary_required = (
        boundary_estimate.total_tokens
        + plan.prompt_tool_overhead_tokens
        + plan.output_reserve_tokens
    )
    assert boundary_required <= plan.slot_context_tokens
    assert plan.slot_context_tokens - boundary_required < 256


def test_offline_verifier_requires_identity_for_every_admitted_model() -> None:
    base = production_plan()
    contexts = {
        "chat": base,
        "coder": replace(base, model_name="coder"),
    }

    def transport(
        method: str, path: str, payload: dict[str, object] | None
    ) -> HttpResponse:
        del method, payload
        if path == "/health":
            return HttpResponse(200, {"status": "ok"}, {}, 1)
        if path == "/v1/models":
            return HttpResponse(
                200,
                {"data": [{"id": "chat"}, {"id": "embed"}]},
                {},
                1,
            )
        if path == "/running":
            return HttpResponse(200, {"running": []}, {}, 1)
        return HttpResponse(503, {"error": "fixture unavailable"}, {}, 1)

    results = serving.run_offline_probes(
        transport=transport,
        contexts=contexts,
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        engine_runner=None,
    )

    identity = next(result for result in results if result.name == "model_identity")
    assert identity.status == FAIL
    assert identity.evidence["missing"] == ["coder"]


def test_probe_capacity_failure_is_fail_not_false_green() -> None:
    def transport(
        _method: str, path: str, _payload: dict[str, object] | None
    ) -> HttpResponse:
        if path in {"/health", "/v1/models", "/running"}:
            body: dict[str, object] = (
                {"data": [{"id": "chat"}, {"id": "embed"}]}
                if path == "/v1/models"
                else {"running": []}
                if path == "/running"
                else {"status": "ok"}
            )
            return HttpResponse(200, body, {}, 1)
        if path == "/v1/chat/completions":
            return HttpResponse(413, {"error": "context exceeds admitted boundary"}, {}, 1)
        return HttpResponse(503, {"error": "fixture unavailable"}, {}, 1)

    results = serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        engine_runner=None,
    )

    boundary = next(result for result in results if result.name == "context_boundary")
    assert boundary.status == FAIL
    assert "admitted" in boundary.reason
    assert serving.aggregate_probe_status(results) == FAIL


def test_health_only_can_never_make_verification_green() -> None:
    results = [
        serving.ProbeResult("health", PASS, "healthy", {}),
        serving.ProbeResult("openai_chat", SKIP, "service not provisioned", {}),
    ]
    assert serving.aggregate_probe_status(results) == PROVISIONAL


def test_probe_evidence_redacts_agent_mcp_and_auth_credentials() -> None:
    raw = {
        "Authorization": "Bearer super-secret-token",
        "OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz123456",
        "nested": {
            "cookie": "session=private",
            "url": "https://user:password@example.invalid/path",
            "safe": "kept",
            "output": (
                "agent ready AGENT_MCP_AUTH_TOKEN=opaque-agent-value "
                "ANTHROPIC_AUTH_TOKEN=opaque-anthropic-value"
            ),
        },
    }
    redacted = serving.redact_sensitive(raw)
    encoded = json.dumps(redacted)

    assert "super-secret-token" not in encoded
    assert "abcdefghijklmnopqrstuvwxyz" not in encoded
    assert "password" not in encoded
    assert "opaque-agent-value" not in encoded
    assert "opaque-anthropic-value" not in encoded
    assert redacted["nested"]["safe"] == "kept"
    assert encoded.count("[REDACTED]") >= 3


def test_firewall_coverage_is_complete_only_in_read_only_inspection() -> None:
    expected = {"editor", "engines", "inference", "python", "node", "package-managers"}
    complete = {name: (f"/fixture/{name}",) for name in expected}

    result = serving.inspect_firewall_coverage(
        expected_classes=expected,
        resolved=complete,
        read_only=True,
    )
    assert result.status == PASS

    incomplete = dict(complete)
    incomplete.pop("inference")
    result = serving.inspect_firewall_coverage(
        expected_classes=expected,
        resolved=incomplete,
        read_only=True,
    )
    assert result.status == FAIL
    assert "inference" in result.reason

    with pytest.raises(ServingError, match="read-only"):
        serving.inspect_firewall_coverage(
            expected_classes=expected,
            resolved=complete,
            read_only=False,
        )


@pytest.mark.parametrize(
    "unsafe",
    ["../escape", "folder/file", r"folder\\file", ".", "..", "x\nname", "/absolute"],
)
def test_envoy_output_names_are_confined(unsafe: str) -> None:
    with pytest.raises(ServingError):
        serving.safe_output_name(unsafe)


def test_envoy_output_names_accept_plain_portable_basename() -> None:
    assert serving.safe_output_name("artifact-1.2.3.tar.gz") == "artifact-1.2.3.tar.gz"


def test_third_party_skill_policy_excludes_and_flags_network_instructions(
    tmp_path: Path,
) -> None:
    put(tmp_path, "vendor/superpowers/skills/tdd/SKILL.md", "# TDD\nRun tests locally.\n")
    put(
        tmp_path,
        "vendor/gstack/deploy/SKILL.md",
        "# Deploy\nUse curl https://cloud.example and gh pr create.\n",
    )
    policy = {
        "schema_version": 1,
        "allow": {
            "superpowers": ["tdd"],
            "gstack": ["deploy"],
        },
        "network_markers": ["https://", "curl ", "gh pr"],
    }
    policy_path = put(tmp_path, "offline-policy.json", json.dumps(policy))

    result = serving.curate_third_party_skills(
        tmp_path / "vendor", policy_path
    )

    assert [item.name for item in result.allowed] == ["sp-tdd"]
    assert [item.name for item in result.flagged] == ["gs-deploy"]
    assert "network-capable instructions" in result.flagged[0].reason


def test_skill_network_markers_match_tokens_not_word_fragments(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    skill = vendor / "superpowers" / "skills" / "local"
    skill.mkdir(parents=True)
    (vendor / "gstack").mkdir()
    (skill / "SKILL.md").write_text(
        "Do enough local work, then report high confidence.\n", encoding="utf-8"
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allow": {"superpowers": ["local"], "gstack": []},
                "network_markers": ["gh "],
            }
        ),
        encoding="utf-8",
    )
    result = serving.curate_third_party_skills(vendor, policy)
    assert [item.name for item in result.allowed] == ["sp-local"]
    assert result.flagged == ()


def test_skill_policy_fails_when_allowlisted_skill_is_missing(tmp_path: Path) -> None:
    put(
        tmp_path,
        "vendor/superpowers/skills/present/SKILL.md",
        "# Present\nLocal work only.\n",
    )
    (tmp_path / "vendor/gstack").mkdir(parents=True)
    policy = put(
        tmp_path,
        "policy.json",
        json.dumps(
            {
                "schema_version": 1,
                "allow": {
                    "superpowers": ["present", "missing"],
                    "gstack": [],
                },
                "network_markers": ["curl"],
            }
        ),
    )
    with pytest.raises(ServingError, match="allowlisted.*missing"):
        serving.curate_third_party_skills(tmp_path / "vendor", policy)


def test_skill_pack_installers_enforce_shared_offline_curation() -> None:
    for relative in (
        "harness/skill-packs/install-skill-packs.ps1",
        "harness/skill-packs/install-skill-packs.sh",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "offline-policy.json" in source
        assert "skill-policy" in source
        assert "flagged" in source.lower()
        assert "allowed" in source.lower()


def test_serving_cli_runs_by_absolute_path_outside_repository(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "verification" / "serving.py"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "capabilities" in completed.stdout


def test_verify_cli_classifies_missing_runtime_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = tmp_path / "private-marker"
    result = serving.main(["verify", "--root", str(private_root)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 1
    assert payload["status"] == FAIL
    assert payload["results"][0]["name"] == "runtime_admission"
    assert payload["results"][0]["status"] == FAIL
    assert "unreadable" in payload["results"][0]["reason"]
    assert "private-marker" not in payload["results"][0]["reason"]
    assert captured.err == ""


def test_generated_admission_fails_closed_on_inconsistent_context_math(
    tmp_path: Path,
) -> None:
    plan = production_plan()
    admission = put(
        tmp_path,
        "admission.json",
        json.dumps(
            {
                "schema_version": 1,
                "models": {
                    "chat": {
                        "nominal_context": plan.nominal_context_tokens,
                        "server_context": (
                            plan.slot_context_tokens * plan.parallel_slots
                        ),
                        "parallel_slots": plan.parallel_slots,
                        "slot_context": plan.slot_context_tokens,
                        "advertised_context": plan.slot_context_tokens,
                        "prompt_tool_overhead": (
                            plan.prompt_tool_overhead_tokens
                        ),
                        "output_reserve": plan.output_reserve_tokens,
                        "model_memory_mib": plan.model_memory_mib,
                        "kv_mib_per_token": plan.kv_mib_per_token,
                        "peak_memory_mib": plan.peak_memory_mib,
                        "usable_memory_mib": plan.usable_memory_mib,
                    }
                },
            }
        ),
    )

    with pytest.raises(ServingError, match="inconsistent context envelope"):
        serving._load_admission(admission)


def test_verify_cli_fails_closed_on_stale_undeclared_admission_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    revision = "a" * 40
    put(
        tmp_path,
        "serving/models.manifest",
        f"chat | example/chat | *.gguf | fast | 65536 | | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n",
    )
    put(
        tmp_path,
        "state/generated/serving/admission.json",
        json.dumps(
            {
                "schema_version": 1,
                "tiers": {
                    "OPUS_MODEL": "unknown",
                    "SONNET_MODEL": "unknown",
                    "HAIKU_MODEL": "unknown",
                },
                "models": {"unknown": admission_fields(production_plan())},
            }
        ),
    )

    result = serving.main(["verify", "--root", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert result == 1
    assert payload["status"] == FAIL
    assert "undeclared" in payload["results"][0]["reason"]


def test_platform_wrappers_delegate_to_shared_core_and_expose_cli_parity() -> None:
    powershell = (REPO_ROOT / "bin/oracle.ps1").read_text(encoding="utf-8")
    posix = (REPO_ROOT / "bin/oracle").read_text(encoding="utf-8")
    win_service = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    mac_service = (REPO_ROOT / "serving/service.sh").read_text(encoding="utf-8")
    render_wrapper = (REPO_ROOT / "bootstrap/render-config.sh").read_text(
        encoding="utf-8"
    )

    for command in ("doctor", "verify", "capabilities", "service"):
        assert f'"{command}"' in powershell or f'"{command}"' in powershell
        assert f"{command})" in posix
    for source in (win_service, mac_service):
        assert "verification" in source
        assert "serving.py" in source
        assert "state/generated/serving" in source.replace("\\", "/")
        assert "127.0.0.1" in source
    assert "AdapterRAM" not in win_service
    assert "nvidia-smi" in win_service
    assert re.search(
        r'"capabilities"\s*\{.{0,500}--backend',
        win_service,
        flags=re.DOTALL,
    )
    assert re.search(
        r"capabilities\).{0,500}--backend",
        mac_service,
        flags=re.DOTALL,
    )
    assert "serving/service.sh" in render_wrapper
    assert "llama-swap.rendered" not in render_wrapper
    assert "desk)" not in posix
    assert '"desk"' not in powershell


def test_service_wrappers_expose_durable_install_start_stop_status_and_restart() -> None:
    windows = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    macos = (REPO_ROOT / "serving/service.sh").read_text(encoding="utf-8")

    for operation in ("install", "uninstall", "start", "stop", "status", "restart"):
        assert operation in windows
        assert operation in macos
    assert "ScheduledTask" in windows
    assert "launchctl" in macos
    assert "pid" in windows.lower()
    assert "pid" in macos.lower()
    assert "[DateTimeOffset]($Process.StartTime.ToUniversalTime())" in windows
    assert "state own-service" in windows
    assert "windows-scheduled-task" in windows
    assert "state own-service" in macos
    assert "launchd-user" in macos


def test_launchd_descriptor_is_atomic_and_preserves_special_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo with spaces & shell"
    output = root / "state/generated/serving/com.sentivue.oracle-serving.plist"
    python = root / "runtime & tools/python"
    llama_swap = root / "runtime & tools/llama-swap"
    config = root / "state/generated/serving/llama-swap.yaml"
    admission = root / "state/generated/serving/admission.json"

    serving.write_launchd_plist(
        output=output,
        label="com.sentivue.oracle-serving",
        python=python,
        root=root,
        llama_swap=llama_swap,
        config=config,
        admission=admission,
        stdout=root / "logs/serving.out.log",
        stderr=root / "logs/serving.err.log",
    )

    payload = plistlib.loads(output.read_bytes())
    assert payload["ProgramArguments"][0] == str(python)
    assert str(root) in payload["ProgramArguments"]
    assert str(llama_swap) in payload["ProgramArguments"]
    assert payload["KeepAlive"] is True
    assert output.read_bytes()[:3] != b"\xef\xbb\xbf"
    assert not list(output.parent.glob("*.tmp"))


def test_service_runner_rejects_tampered_generated_config_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llama_swap = put(tmp_path, "tools/llama-swap", b"fixture")
    config = put(
        tmp_path,
        "state/generated/serving/llama-swap.yaml",
        "models: {}\n",
    )
    admission = put(
        tmp_path,
        "state/generated/serving/admission.json",
        "{}\n",
    )
    monkeypatch.setattr(
        serving,
        "_load_admission",
        lambda _path: (
            {"models": {}, "config_sha256": "0" * 64},
            {"chat": production_plan()},
        ),
    )
    monkeypatch.setattr(
        serving.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "tampered config must be rejected before child start"
        ),
    )

    with pytest.raises(ServingError, match="config integrity"):
        serving._service_run(
            root=tmp_path,
            llama_swap=llama_swap,
            config=config,
            admission_path=admission,
            gateway_host="127.0.0.1",
            gateway_port=9099,
            upstream_host="127.0.0.1",
            upstream_port=9098,
        )


def test_service_runner_stops_gateway_when_llama_swap_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llama_swap = put(tmp_path, "tools/llama-swap", b"fixture")
    config = put(tmp_path, "state/generated/serving/llama-swap.yaml", "models: {}\n")
    admission = put(tmp_path, "state/generated/serving/admission.json", "{}\n")
    stopped = threading.Event()

    class FakeChild:
        returncode = 7

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

        def terminate(self) -> None:
            raise AssertionError("an exited child must not be terminated")

    class FakeServer:
        def serve_forever(self, poll_interval: float) -> None:
            del poll_interval
            assert stopped.wait(1), "gateway was not stopped after child exit"

        def shutdown(self) -> None:
            stopped.set()

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(
        serving,
        "_load_admission",
        lambda _path: (
            {
                "models": {},
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            },
            {"chat": production_plan()},
        ),
    )
    monkeypatch.setattr(serving.subprocess, "Popen", lambda *args, **kwargs: FakeChild())
    monkeypatch.setattr(serving, "create_admission_server", lambda **kwargs: FakeServer())
    monkeypatch.setattr(serving.signal, "signal", lambda *_args: None)

    result = serving._service_run(
        root=tmp_path,
        llama_swap=llama_swap,
        config=config,
        admission_path=admission,
        gateway_host="127.0.0.1",
        gateway_port=9099,
        upstream_host="127.0.0.1",
        upstream_port=9098,
    )

    assert result == 7
    assert stopped.is_set()


def test_service_runner_cleans_pid_and_logs_when_child_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llama_swap = put(tmp_path, "tools/llama-swap", b"fixture")
    config = put(tmp_path, "state/generated/serving/llama-swap.yaml", "models: {}\n")
    admission = put(tmp_path, "state/generated/serving/admission.json", "{}\n")
    monkeypatch.setattr(
        serving,
        "_load_admission",
        lambda _path: (
            {
                "models": {},
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            },
            {"chat": production_plan()},
        ),
    )

    def fail_start(*_args: object, **_kwargs: object) -> None:
        raise OSError("fixture start failure")

    monkeypatch.setattr(serving.subprocess, "Popen", fail_start)

    with pytest.raises(ServingError, match="could not start"):
        serving._service_run(
            root=tmp_path,
            llama_swap=llama_swap,
            config=config,
            admission_path=admission,
            gateway_host="127.0.0.1",
            gateway_port=9099,
            upstream_host="127.0.0.1",
            upstream_port=9098,
        )

    assert not (tmp_path / "state/generated/serving/service.pid.json").exists()
    (tmp_path / "logs/llama-swap.out.log").unlink()
    (tmp_path / "logs/llama-swap.err.log").unlink()


def test_service_runner_stops_child_when_gateway_bind_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llama_swap = put(tmp_path, "tools/llama-swap", b"fixture")
    config = put(tmp_path, "state/generated/serving/llama-swap.yaml", "models: {}\n")
    admission = put(tmp_path, "state/generated/serving/admission.json", "{}\n")

    class FakeChild:
        returncode: int | None = None
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return int(self.returncode or 0)

    child = FakeChild()
    monkeypatch.setattr(
        serving,
        "_load_admission",
        lambda _path: (
            {
                "models": {},
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            },
            {"chat": production_plan()},
        ),
    )
    monkeypatch.setattr(serving.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(
        serving,
        "create_admission_server",
        lambda **kwargs: (_ for _ in ()).throw(OSError("address in use")),
    )

    with pytest.raises(ServingError, match="gateway"):
        serving._service_run(
            root=tmp_path,
            llama_swap=llama_swap,
            config=config,
            admission_path=admission,
            gateway_host="127.0.0.1",
            gateway_port=9099,
            upstream_host="127.0.0.1",
            upstream_port=9098,
        )

    assert child.terminated
    assert not (tmp_path / "state/generated/serving/service.pid.json").exists()
    (tmp_path / "logs/llama-swap.out.log").unlink()
    (tmp_path / "logs/llama-swap.err.log").unlink()


def test_envoy_fetch_disallows_redirects_and_delegates_output_confinement() -> None:
    source = (REPO_ROOT / "bin/envoy-fetch").read_text(encoding="utf-8")

    assert "--max-redirs 0" in source
    assert "serving.py" in source
    assert "safe-output" in source
    assert "curl -fL" not in source


def test_doctors_use_shared_read_only_probes_and_do_not_claim_nominal_context() -> None:
    for relative in ("bootstrap/doctor.ps1", "bootstrap/doctor.sh"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "serving.py" in source
        assert "capabilities" in source
        assert "verify" in source
        assert "advertised_context" in source
        assert "read-only" in source.lower()
        assert source.count("loaded_backend") >= 2
        assert re.search(
            r"(?:name.{0,80}loaded_backend|loaded_backend.{0,80}name)",
            source,
            flags=re.DOTALL,
        )
        assert "tier collapse" in source.lower()


def test_security_audits_check_shared_loopback_enforcement() -> None:
    for relative in (
        "bootstrap/security-audit.ps1",
        "bootstrap/security-audit.sh",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "verification/serving.py" in source.replace("\\", "/")
        assert "require_loopback" in source


def test_docs_mark_runtime_certification_provisional_and_remove_dead_claims() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    platform_map = (REPO_ROOT / "docs/PLATFORM-MAP.md").read_text(encoding="utf-8")

    assert "runtime certification is provisional" in readme.lower()
    assert "both first-class" not in readme.lower()
    assert "native desktop app" not in readme.lower()
    assert "hardware-adaptive placement" not in platform_map.lower()
