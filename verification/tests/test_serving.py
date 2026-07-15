from __future__ import annotations

import hashlib
import http.client
import inspect
import json
import math
import plistlib
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
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


def test_large_nominal_context_grants_large_window_when_memory_allows() -> None:
    # A wide nominal window (qwen3-coder-30b's 262144 native max) on a
    # large-memory Metal machine must let admission grant a substantially larger
    # advertised context than the old 65536 nominal ever could.
    plan = serving.plan_context(
        model_name="qwen3-coder-30b",
        nominal_context_tokens=262144,
        requested_parallel=1,
        model_memory_mib=40 * GIB,
        usable_memory_mib=40 * GIB + 70000,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.25,
    )

    assert plan.slot_context_tokens == 262144
    assert plan.advertised_context_tokens >= 200000
    assert plan.peak_memory_mib <= plan.usable_memory_mib


def test_large_nominal_context_reduces_gracefully_on_constrained_memory() -> None:
    # The same wide nominal on a memory-starved machine must never over-commit:
    # admission drops parallelism and caps the slot to what KV memory allows,
    # yielding a small-but-valid advertised context rather than raising.
    kwargs = dict(
        model_name="qwen3-coder-30b",
        requested_parallel=2,
        model_memory_mib=40 * GIB,
        usable_memory_mib=40 * GIB + 8000,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.25,
    )
    wide = serving.plan_context(nominal_context_tokens=262144, **kwargs)

    # Parallelism was reduced and the slot is memory-bound, not nominal-bound.
    assert wide.parallel_slots == 1
    assert wide.slot_context_tokens < 262144
    assert wide.advertised_context_tokens < 65536
    assert wide.advertised_context_tokens >= serving.MINIMUM_ADVERTISED_CONTEXT
    assert wide.peak_memory_mib <= wide.usable_memory_mib

    # On this constrained machine memory is the binding constraint, so raising
    # the nominal from 65536 to 262144 changes nothing: it cannot over-commit.
    narrow = serving.plan_context(nominal_context_tokens=65536, **kwargs)
    assert wide.advertised_context_tokens == narrow.advertised_context_tokens
    assert wide.slot_context_tokens == narrow.slot_context_tokens


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
            {"messages": [{"role": "user", "content": "x " * 210000}]},
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
    # CPU placement keeps the KV cache in system RAM.
    assert "--no-kv-offload" in argv
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


def test_admission_controller_rejects_contention_and_oversize_before_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = production_plan()
    controller = serving.AdmissionController({"chat": plan})
    first = controller.try_begin(
        "chat",
        {"messages": [{"role": "user", "content": "safe request"}]},
    )
    forwarded: list[str] = []

    # The admission queue would otherwise hold this held-slot probe for a full
    # minute; drop the timeout to zero so contention is still rejected promptly.
    monkeypatch.setattr(serving, "ADMISSION_QUEUE_TIMEOUT_SECONDS", 0)
    with pytest.raises(ServingError, match="contention"):
        controller.try_begin(
            "chat",
            {"messages": [{"role": "user", "content": "second request"}]},
        )
    with pytest.raises(ServingError, match="context"):
        controller.try_begin(
            "chat",
            {"messages": [{"role": "user", "content": "x " * 210000}]},
        )
    assert forwarded == []

    first.close()
    second = controller.try_begin(
        "chat",
        {"messages": [{"role": "user", "content": "safe again"}]},
    )
    second.close()


def test_admission_controller_rejects_cross_model_exclusive_group_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    # Reject the held exclusive group immediately instead of queuing for a minute.
    monkeypatch.setattr(serving, "ADMISSION_QUEUE_TIMEOUT_SECONDS", 0)
    with pytest.raises(ServingError, match="contention"):
        controller.try_begin(
            "big-b", {"messages": [{"role": "user", "content": "work"}]}
        )
    first.close()
    second = controller.try_begin(
        "big-b", {"messages": [{"role": "user", "content": "work"}]}
    )
    second.close()


def test_admission_controller_queues_concurrent_request_until_slot_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A concurrent request for a busy slot now briefly queues instead of being
    # rejected outright: once the in-flight lease is released the queued
    # acquisition succeeds rather than raising a 429-style contention error.
    monkeypatch.setattr(serving, "ADMISSION_QUEUE_TIMEOUT_SECONDS", 5)
    controller = serving.AdmissionController({"chat": production_plan()})
    first = controller.try_begin(
        "chat", {"messages": [{"role": "user", "content": "in flight"}]}
    )

    outcome: list[object] = []
    started = threading.Event()

    def queued() -> None:
        started.set()
        try:
            outcome.append(
                controller.try_begin(
                    "chat",
                    {"messages": [{"role": "user", "content": "queued"}]},
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            outcome.append(exc)

    worker = threading.Thread(target=queued)
    worker.start()
    assert started.wait(timeout=5)
    # While the single slot is held the queued request is still waiting, not errored.
    time.sleep(0.2)
    assert worker.is_alive()
    assert outcome == []

    # Releasing the in-flight lease lets the queued acquisition through.
    first.close()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    lease = outcome[0]
    assert isinstance(lease, serving.AdmissionLease)
    lease.close()


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
                "messages": [{"role": "user", "content": "x " * 210000}],
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
    running_calls = 0

    def transport(
        method: str, path: str, payload: dict[str, object] | None
    ) -> HttpResponse:
        nonlocal running_calls
        requests.append((method, path, payload))
        if path == "/health":
            return HttpResponse(200, {"status": "ok"}, {}, 5)
        if path == "/v1/models":
            return HttpResponse(200, {"data": [{"id": "chat"}, {"id": "embed"}]}, {}, 5)
        if path == "/running":
            running_calls += 1
            if running_calls == 1:
                return HttpResponse(200, {"running": []}, {}, 5)
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
            if payload and str(payload.get("input", "")).startswith(
                "oversize-embedding "
            ):
                return HttpResponse(
                    413,
                    {"error": "rejected before upstream"},
                    {"X-Oracle-Admission": "rejected"},
                    1,
                )
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
                {
                    "choices": [{"message": {"content": "ORACLE-OK"}}],
                    "usage": {"prompt_tokens": 25000},
                },
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
    # The probe still sends a ~25k-token production-shaped prompt; the estimator
    # now sizes it in tokens (bytes / CONSERVATIVE_BYTES_PER_TOKEN) rather than
    # raw bytes, so its estimate is a smaller but still substantial token count.
    assert estimate.prompt_tokens >= 16000
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


def test_offline_probe_progress_reports_each_phase() -> None:
    def transport(method: str, path: str, body: object = None) -> HttpResponse:
        return HttpResponse(200, {"status": "ok", "data": [], "running": []}, {}, 1)

    messages: list[str] = []
    serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        engine_runner=lambda engine: (0, "ENGINE-OK"),
        progress=messages.append,
    )
    joined = "\n".join(messages)
    assert "checking the local service and model list" in joined
    assert "probing the chat model" in joined
    assert "probing the embedding model" in joined
    assert "probing headless engine sessions" in joined

    without_engine: list[str] = []
    serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        engine_runner=None,
        progress=without_engine.append,
    )
    assert not any("headless engine sessions" in message for message in without_engine)


def test_detect_resources_metal_uses_default_working_set_when_wired_limit_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # iogpu.wired_limit_mb defaults to 0 ("system managed"); that must NOT be
    # read as zero GPU memory (which forced CPU on every un-raised Mac).
    monkeypatch.setattr(serving.host_platform, "system", lambda: "Darwin")
    monkeypatch.setattr(serving, "_system_memory_mib", lambda: (262144, 240000))
    monkeypatch.setattr(serving, "_macos_wired_limit_mib", lambda: 0)
    monkeypatch.setattr(serving, "_nvidia_smi_output", lambda: None)
    monkeypatch.setattr(serving, "_vulkan_available", lambda: False)

    snapshot = serving.detect_resources("auto")

    assert snapshot.backend == serving.Backend.METAL
    assert snapshot.accelerator_shared is True
    assert snapshot.accelerator_available_mib == min(240000, 262144 * 70 // 100)
    assert snapshot.accelerator_available_mib > 0


def test_detect_resources_metal_honors_explicit_wired_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serving.host_platform, "system", lambda: "Darwin")
    monkeypatch.setattr(serving, "_system_memory_mib", lambda: (262144, 240000))
    monkeypatch.setattr(serving, "_macos_wired_limit_mib", lambda: 196608)
    monkeypatch.setattr(serving, "_nvidia_smi_output", lambda: None)
    monkeypatch.setattr(serving, "_vulkan_available", lambda: False)

    snapshot = serving.detect_resources("auto")

    assert snapshot.accelerator_available_mib == min(240000, 196608)


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
    # macOS throttles "Background" ProcessType jobs off the GPU, so the service
    # must run "Interactive" to keep the model server on the Metal accelerator.
    assert payload["ProcessType"] == "Interactive"
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


def test_review_token_admission_is_a_recursive_utf8_upper_bound() -> None:
    payload: dict[str, object] = {
        "model": "chat",
        "max_tokens": 257,
        "system": "policy:\n" + ("![]{}();λ🙂" * 400),
        "messages": [
            {
                "role": "assistant",
                "content": "x:=[]{}();\n" * 700,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "run",
                            "arguments": json.dumps(
                                {"unicode": "🙂" * 500, "code": "a+=1;" * 900}
                            ),
                        },
                    }
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "run",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "opaque": {
                                "type": "string",
                                "description": "~!@#$%^&*()" * 500,
                            }
                        },
                    },
                },
            }
        ],
        "unknown_protocol_extension": {
            "nested": [{"punctuation": "::::;;;;" * 600}]
        },
    }

    estimate = serving.estimate_request_tokens(payload)
    # The estimator canonicalizes the whole request recursively (including tool
    # calls nested inside messages and unknown extension fields) and sizes it
    # with a conservative bytes-per-token ratio. The estimate must never fall
    # below bytes / ratio, which itself stays above the real byte-level token
    # count, so admission remains a safe over-estimate of true tokens.
    canonical_bytes = len(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    conservative_floor = serving._bytes_to_tokens(canonical_bytes)

    assert estimate.prompt_tokens == conservative_floor
    assert estimate.prompt_tokens + estimate.tool_schema_tokens >= conservative_floor
    assert estimate.output_tokens == 257


def test_review_token_admission_keeps_ordinary_25k_request_usable() -> None:
    # ~25k tokens of content: sized in tokens, 75k bytes / CONSERVATIVE_BYTES_PER_TOKEN
    # lands around 25k, so an ordinary agent request stays comfortably admissible
    # instead of being inflated ~4x by counting every byte as a token.
    estimate = serving.estimate_request_tokens(
        {
            "model": "chat",
            "messages": [{"role": "user", "content": "a" * 75000}],
            "max_tokens": 128,
        }
    )

    assert 25000 <= estimate.prompt_tokens < 30000
    with pytest.raises(ServingError, match="JSON|binary|unsupported"):
        serving.estimate_request_tokens(
            {"messages": [{"role": "user", "content": b"\x00\xff"}]}
        )


def test_request_estimator_sizes_in_tokens_not_bytes() -> None:
    # Whole bytes round up to whole tokens at the conservative ratio.
    assert serving.CONSERVATIVE_BYTES_PER_TOKEN == 3
    assert serving._bytes_to_tokens(0) == 0
    assert serving._bytes_to_tokens(3000) == 1000
    assert serving._bytes_to_tokens(3001) == 1001
    assert serving._bytes_to_tokens(3002) == 1001
    # A known ASCII string is sized as ceil(bytes / 3), not one token per byte.
    assert serving._estimate_text_tokens("a" * 3000) == 1000

    plan = production_plan()
    controller = serving.AdmissionController({"chat": plan})

    # A request whose raw UTF-8 byte length exceeds the slot context but whose
    # token estimate (bytes / 3) still fits is now admitted. Under the old
    # byte==token estimator this ordinary request was wrongly rejected with 413.
    fits_in_tokens = {
        "model": "chat",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": "a" * (plan.slot_context_tokens + 4096)}
        ],
    }
    serialized_bytes = len(
        json.dumps(
            fits_in_tokens, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    assert serialized_bytes > plan.slot_context_tokens
    estimate = serving.estimate_request_tokens(fits_in_tokens)
    assert (
        estimate.total_tokens
        + plan.prompt_tool_overhead_tokens
        + plan.output_reserve_tokens
        <= plan.slot_context_tokens
    )
    lease = controller.try_begin("chat", fits_in_tokens)
    lease.close()

    # A genuinely token-oversize request (bytes / 3 already exceeds the budget)
    # is still rejected before it can reach the model.
    with pytest.raises(ServingError, match="context"):
        controller.try_begin(
            "chat",
            {
                "model": "chat",
                "max_tokens": 128,
                "messages": [
                    {
                        "role": "user",
                        "content": "a"
                        * (
                            serving.CONSERVATIVE_BYTES_PER_TOKEN
                            * plan.slot_context_tokens
                        ),
                    }
                ],
            },
        )


def test_review_context_reduces_parallelism_to_preserve_25k_envelope() -> None:
    plan = serving.plan_context(
        model_name="chat",
        nominal_context_tokens=65536,
        requested_parallel=2,
        model_memory_mib=8 * GIB,
        usable_memory_mib=48 * GIB,
        prompt_tool_overhead_tokens=4096,
        output_reserve_tokens=4096,
        kv_mib_per_token=0.0625,
        minimum_advertised_context=25000,
    )

    assert plan.parallel_slots == 1
    assert plan.advertised_context_tokens >= 25000


def test_review_backend_placement_fits_ram_vram_and_renders_finite_offload() -> None:
    assert hasattr(serving, "plan_model_placement")
    assert hasattr(serving, "PlacementPlan")
    resources = ResourceSnapshot(
        system_total_mib=32 * GIB,
        system_available_mib=30 * GIB,
        backend=Backend.CUDA,
        accelerator_total_mib=12 * GIB,
        accelerator_available_mib=11 * GIB,
        accelerator_shared=False,
        capability_source="fixture exact VRAM",
    )
    placement = serving.plan_model_placement(
        model_name="chat",
        model_memory_mib=16 * GIB,
        layer_mib=(512,) * 32,
        kv_runtime_mib=1024,
        resources=resources,
        requested_backend="cuda",
    )

    assert placement.backend == Backend.CUDA
    assert 0 < placement.offloaded_layers < 999
    assert placement.ram_required_mib <= (
        resources.system_available_mib
        - resources.os_reserve_mib
        - resources.runtime_reserve_mib
    )
    assert placement.vram_required_mib <= (
        resources.accelerator_available_mib - resources.accelerator_reserve_mib
    )
    assert placement.split_mode == "layer"


def test_review_unknown_vulkan_vram_cannot_justify_gpu_placement() -> None:
    assert hasattr(serving, "plan_model_placement")
    resources = ResourceSnapshot(
        system_total_mib=32 * GIB,
        system_available_mib=30 * GIB,
        backend=Backend.VULKAN,
        capability_source="Vulkan memory unknown",
    )

    fallback = serving.plan_model_placement(
        model_name="chat",
        model_memory_mib=8 * GIB,
        layer_mib=None,
        kv_runtime_mib=1024,
        resources=resources,
        requested_backend="auto",
    )
    assert fallback.backend == Backend.CPU
    assert fallback.offloaded_layers == 0
    with pytest.raises(ServingError, match="VRAM|placement"):
        serving.plan_model_placement(
            model_name="chat",
            model_memory_mib=8 * GIB,
            layer_mib=None,
            kv_runtime_mib=1024,
            resources=resources,
            requested_backend="vulkan",
        )


def test_review_service_freshness_rechecks_models_config_resources_and_binaries(
    tmp_path: Path,
) -> None:
    assert hasattr(serving, "validate_runtime_freshness")
    root = tmp_path / "runtime trust"
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
        f"chat | example/chat | *.gguf | fast | 65536 | | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n",
    )
    put(root, "serving/models.profile", "chat\nembed\n")
    put(
        root,
        "serving/tiers.env",
        "OPUS_MODEL=chat\nSONNET_MODEL=chat\nHAIKU_MODEL=chat\n",
    )
    policy_bound_fixture(root)
    server = put(root, ".tools/bin/llama-server", b"trusted server")
    llama_swap = put(root, ".tools/bin/llama-swap", b"trusted swap")
    resources = ResourceSnapshot(
        system_total_mib=48 * GIB,
        system_available_mib=44 * GIB,
        backend=Backend.CPU,
        capability_source="fixture",
    )
    runtime = serving.prepare_runtime(
        root=root,
        server_path=server,
        llama_swap_path=llama_swap,
        platform="posix",
        resources=resources,
        requested_backend="cpu",
    )

    serving.validate_runtime_freshness(
        root=root,
        config=runtime.rendered.path,
        admission_path=runtime.rendered.metadata_path,
        llama_swap=llama_swap,
        resources=resources,
    )

    put(root, "serving/models.profile", "chat\n")
    with pytest.raises(ServingError, match="active profile|active models"):
        serving.validate_runtime_freshness(
            root=root,
            config=runtime.rendered.path,
            admission_path=runtime.rendered.metadata_path,
            llama_swap=llama_swap,
            resources=resources,
        )
    put(root, "serving/models.profile", "chat\nembed\n")

    model_path = root / "models/chat/chat.gguf"
    model_path.write_bytes(b"changed GGUF")
    with pytest.raises(ServingError, match="model|digest|size"):
        serving.validate_runtime_freshness(
            root=root,
            config=runtime.rendered.path,
            admission_path=runtime.rendered.metadata_path,
            llama_swap=llama_swap,
            resources=resources,
        )
    model_path.write_bytes(b"GGUF chat fixture")

    original_config = runtime.rendered.path.read_bytes()
    runtime.rendered.path.write_bytes(original_config + b"# changed\n")
    with pytest.raises(ServingError, match="config"):
        serving.validate_runtime_freshness(
            root=root,
            config=runtime.rendered.path,
            admission_path=runtime.rendered.metadata_path,
            llama_swap=llama_swap,
            resources=resources,
        )
    runtime.rendered.path.write_bytes(original_config)

    low_resources = replace(resources, system_available_mib=4 * GIB)
    with pytest.raises(ServingError, match="resource|memory|RAM"):
        serving.validate_runtime_freshness(
            root=root,
            config=runtime.rendered.path,
            admission_path=runtime.rendered.metadata_path,
            llama_swap=llama_swap,
            resources=low_resources,
        )

    llama_swap.write_bytes(b"changed swap")
    with pytest.raises(ServingError, match="binary|llama-swap"):
        serving.validate_runtime_freshness(
            root=root,
            config=runtime.rendered.path,
            admission_path=runtime.rendered.metadata_path,
            llama_swap=llama_swap,
            resources=resources,
        )


def test_review_installed_runtime_binary_must_match_policy_archive(
    tmp_path: Path,
) -> None:
    assert hasattr(serving, "validate_binary_against_archive")
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("runtime/bin/llama-server.exe", b"trusted executable")
    installed = put(tmp_path, "tools/llama-server.exe", b"trusted executable")

    serving.validate_binary_against_archive(
        installed=installed,
        archive=archive,
        member_name="llama-server.exe",
    )
    installed.write_bytes(b"changed executable")
    with pytest.raises(ServingError, match="provenance|archive|binary"):
        serving.validate_binary_against_archive(
            installed=installed,
            archive=archive,
            member_name="llama-server.exe",
        )


def test_review_runtime_evidence_checks_cold_warm_backend_offload_and_all_listeners() -> None:
    parameters = inspect.signature(serving.run_offline_probes).parameters
    assert "planned_placements" in parameters
    assert "listener_inspector" in parameters
    running_calls = 0

    def transport(
        method: str, path: str, payload: dict[str, object] | None
    ) -> HttpResponse:
        nonlocal running_calls
        del method, payload
        if path == "/health":
            return HttpResponse(200, {"status": "ok"}, {}, 1)
        if path == "/v1/models":
            return HttpResponse(
                200, {"data": [{"id": "chat"}, {"id": "embed"}]}, {}, 1
            )
        if path == "/running":
            running_calls += 1
            if running_calls == 1:
                return HttpResponse(200, {"running": []}, {}, 1)
            return HttpResponse(
                200,
                {
                    "running": [
                        {
                            "model": "chat",
                            "state": "ready",
                            "backend": "cpu",
                            "offloaded_layers": 0,
                            "port": 19001,
                        }
                    ]
                },
                {},
                1,
            )
        return HttpResponse(503, {"error": "fixture unavailable"}, {}, 1)

    results = serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=(),
        listener_inspector=lambda port: {
            9099: ("127.0.0.1:9099",),
            9098: ("0.0.0.0:9098",),
            19001: ("127.0.0.1:19001",),
        }.get(port, ()),
        expected_listener_ports={"public": 9099, "internal": 9098},
        planned_placements={
            "chat": {"backend": "cuda", "offloaded_layers": 8}
        },
        engine_runner=None,
    )

    by_name = {result.name: result for result in results}
    assert by_name["cold_warm_state"].status == PASS
    assert by_name["loaded_backend"].status == FAIL
    assert by_name["loopback_binding"].status == FAIL
    assert running_calls >= 2


def test_review_gateway_rejects_redirect_without_credential_forwarding() -> None:
    leak_calls: list[str] = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(302)
                self.send_header("Location", "/leak")
                self.end_headers()
                return
            leak_calls.append(self.headers.get("Authorization", ""))
            body = b'{"leaked":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    gateway = serving.create_admission_server(
        host="127.0.0.1",
        port=0,
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        contexts={"chat": production_plan()},
        evidence={},
    )
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        connection.request(
            "GET", "/health", headers={"Authorization": "Bearer must-not-follow"}
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 503
        assert leak_calls == []
    finally:
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()


def test_review_envoy_fetch_rejects_local_redirect_behaviorally(
    tmp_path: Path,
) -> None:
    assert hasattr(serving, "fetch_url_no_redirect")
    redirect_hits: list[str] = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/artifact":
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            redirect_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unexpected")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "artifact.bin"
    try:
        with pytest.raises(ServingError, match="redirect|HTTP 302"):
            serving.fetch_url_no_redirect(
                url=f"http://127.0.0.1:{server.server_port}/artifact",
                output=output,
                allowed_hosts={"127.0.0.1"},
                allowed_schemes={"http"},
                timeout=5,
                maximum_bytes=1024,
            )
        assert not output.exists()
        assert redirect_hits == []
    finally:
        server.shutdown()
        server.server_close()


def test_review_gateway_streams_first_chunk_before_upstream_completion() -> None:
    first_upstream_chunk = threading.Event()
    release_upstream = threading.Event()
    first_gateway_chunk = threading.Event()
    received: list[bytes] = []

    class StreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Request-Id", "stream-fixture")
            self.send_header("Set-Cookie", "secret=must-not-forward")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            first_upstream_chunk.set()
            release_upstream.wait(5)
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    gateway = serving.create_admission_server(
        host="127.0.0.1",
        port=0,
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        contexts={"chat": production_plan()},
        evidence={},
    )
    threading.Thread(target=gateway.serve_forever, daemon=True).start()

    def client() -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=10
        )
        body = json.dumps(
            {
                "model": "chat",
                "stream": True,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "stream"}],
            }
        )
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.getheader("Set-Cookie") is None
        assert response.getheader("Connection", "").lower() != "close"
        received.append(response.read(13))
        first_gateway_chunk.set()
        received.append(response.read())
        connection.close()

    client_thread = threading.Thread(target=client, daemon=True)
    client_thread.start()
    delivered_before_completion = False
    try:
        assert first_upstream_chunk.wait(3)
        delivered_before_completion = first_gateway_chunk.wait(0.5)
    finally:
        release_upstream.set()
        client_thread.join(5)
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()
    assert delivered_before_completion
    assert b"data: first" in b"".join(received)


def test_review_aliases_admit_canonical_plan_and_preserve_requested_identity() -> None:
    assert "aliases" in inspect.signature(
        serving.create_admission_server
    ).parameters
    received_models: list[str] = []

    class AliasHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            received_models.append(payload["model"])
            if self.path == "/v1/embeddings":
                body = b'{"data":[{"embedding":[0,1,2,3,4,5,6,7]}]}'
            else:
                body = b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), AliasHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    embed_plan = replace(production_plan(), model_name="embed")
    gateway = serving.create_admission_server(
        host="127.0.0.1",
        port=0,
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        contexts={"chat": production_plan(), "embed": embed_plan},
        aliases={"gpt-4o": "chat", "text-embedding-3-large": "embed"},
        evidence={},
    )
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        for path, model, payload in (
            (
                "/v1/chat/completions",
                "gpt-4o",
                {"messages": [{"role": "user", "content": "alias"}]},
            ),
            (
                "/v1/embeddings",
                "text-embedding-3-large",
                {"input": "alias embedding"},
            ),
        ):
            request_body = json.dumps({"model": model, **payload})
            connection = http.client.HTTPConnection(
                "127.0.0.1", gateway.server_port, timeout=5
            )
            connection.request(
                "POST",
                path,
                body=request_body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 200
        assert received_models == ["gpt-4o", "text-embedding-3-large"]
        with pytest.raises(ServingError, match="alias|collision"):
            serving.create_admission_server(
                host="127.0.0.1",
                port=0,
                upstream=f"http://127.0.0.1:{upstream.server_port}",
                contexts={"chat": production_plan(), "embed": embed_plan},
                aliases={"chat": "embed"},
                evidence={},
            )
    finally:
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()


def test_review_service_singleton_lock_is_atomic_and_owner_conditional(
    tmp_path: Path,
) -> None:
    assert hasattr(serving, "acquire_service_lock")
    assert hasattr(serving, "release_service_lock")
    state = tmp_path / "state"
    record = PidRecord(4242, "/trusted/python", 100.0, "a" * 64)

    def inspector(_pid: int) -> tuple[str, float, str]:
        return ("/trusted/python", 100.0, "a" * 64)

    owner = serving.acquire_service_lock(state, record, inspect=inspector)
    with pytest.raises(ServingError, match="already|active|owner"):
        serving.acquire_service_lock(state, record, inspect=inspector)
    assert serving.release_service_lock(state, "wrong-owner") is False
    assert (state / "service.lock").exists()
    assert serving.release_service_lock(state, owner) is True
    assert (state / "service.lock").exists()

    stale = serving.acquire_service_lock(state, record, inspect=lambda _pid: None)
    with pytest.raises(ServingError, match="held|owner|race"):
        serving.acquire_service_lock(
            state,
            replace(record, pid=4343),
            inspect=lambda _pid: None,
        )
    assert serving.release_service_lock(state, stale) is True
    replacement = serving.acquire_service_lock(
        state,
        replace(record, pid=4343),
        inspect=lambda _pid: None,
    )
    assert replacement != stale
    assert serving.release_service_lock(state, stale) is False
    assert serving.release_service_lock(state, replacement) is True


def test_review_redaction_covers_token_variants_and_preserves_url_shape() -> None:
    raw = {
        "token": "token-value",
        "access_token": "access-value",
        "refresh-token": "refresh-value",
        "bearer": "bearer-value",
        "Cookie": "session=private",
        "client_secret": "secret-value",
        "password": "password-value",
        "api-key": "api-value",
        "auth_key": "auth-value",
        "nested": [
            "Authorization: Bearer opaque-value",
            "access_token=inline-value",
            "https://user:password@example.invalid/private/path",
        ],
        "safe": "kept",
    }

    redacted = serving.redact_sensitive(raw)
    encoded = json.dumps(redacted)

    for secret in (
        "token-value",
        "access-value",
        "refresh-value",
        "bearer-value",
            "session=private",
        "secret-value",
        "password-value",
        "api-value",
        "auth-value",
        "opaque-value",
        "inline-value",
        "user:password",
    ):
        assert secret not in encoded
    assert redacted["safe"] == "kept"
    assert "https://[REDACTED]@example.invalid/private/path" in encoded


def test_review_doctors_are_offline_and_wrappers_verify_owned_service_identity() -> None:
    doctor = (REPO_ROOT / "bootstrap/doctor.sh").read_text(encoding="utf-8")
    windows = (REPO_ROOT / "serving/serve-windows.ps1").read_text(encoding="utf-8")
    macos = (REPO_ROOT / "serving/service.sh").read_text(encoding="utf-8")

    assert "git fetch" not in doctor
    assert "Test-OwnedScheduledTask" in windows
    assert "Get-ServiceArguments" in windows
    assert "cmp -s" in macos
    assert "refusing" in macos.lower()


def test_start_service_adopts_a_preexisting_same_label_plist_on_reinstall() -> None:
    macos = (REPO_ROOT / "serving/service.sh").read_text(encoding="utf-8")
    start = macos.split("start_service()", 1)[1].split("\nstatus_service()", 1)[0]

    # The launchd label is Oracle-specific, so a leftover plist from a previous
    # install must be adopted, not refused. start must generate the descriptor
    # BEFORE any ownership check and never call verify_owned_launchd (the source
    # of the "ownership descriptor is missing" reinstall abort).
    assert "verify_owned_launchd" not in start
    assert "ownership descriptor is missing" not in macos
    for step in ("render", "sync_engine_configs", "write_generated_plist"):
        assert step in start
    # Publish (adopt/replace) when the plist is absent OR differs from generated.
    assert 'if [[ ! -f "$PLIST" ]] || ! cmp -s "$GENERATED_PLIST" "$PLIST"; then' in start
    assert "publish_plist" in start
    # Ownership is registered and the same-label job is booted out then in.
    assert 'state own --root "$ROOT"' in start
    assert "state own-service" in start
    assert "launchd-user" in start
    assert 'launchctl bootout "gui/$(id -u)/$LABEL"' in start
    assert 'launchctl bootstrap "gui/$(id -u)" "$PLIST"' in start
    # A missing generated descriptor must no longer hard-fail ownership checks.
    verify = macos.split("verify_owned_launchd()", 1)[1].split("\npublish_plist()", 1)[0]
    assert "ownership descriptor is missing" not in verify


def test_review_readme_uses_real_profiles_and_no_unsupported_installer_wizard() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    installer_section = readme.split("## Install", 1)[1].split(
        "## Quickstart", 1
    )[0]

    for profile in ("full", "coder", "mid", "lite", "micro"):
        assert profile in readme
    assert "minimal" not in installer_section
    assert "wizard prompts" not in installer_section


def test_final_review_admission_bounds_implicit_output_and_rejects_bypass_fields() -> None:
    estimate = serving.estimate_request_tokens(
        {"model": "chat", "messages": [{"role": "user", "content": "hello"}]}
    )
    assert estimate.output_tokens == serving.DEFAULT_REQUEST_OUTPUT_TOKENS

    controller = serving.AdmissionController({"chat": production_plan()})
    for bypass in (
        {"n": 2},
        {"n_predict": 1},
        {"max_output_tokens": 1},
        {"max_tokens": 0},
    ):
        with pytest.raises(
            ServingError,
            match="unsupported|completion|output|max_tokens",
        ):
            controller.try_begin(
                    "chat",
                {
                    "model": "chat",
                    "messages": [{"role": "user", "content": "hello"}],
                    **bypass,
                }
            )


def test_final_review_gateway_injects_bounded_default_output() -> None:
    received: list[dict[str, object]] = []

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            body = b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    gateway = serving.create_admission_server(
        host="127.0.0.1",
        port=0,
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        contexts={"chat": production_plan()},
        evidence={},
    )
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", gateway.server_port, timeout=5
        )
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "model": "chat",
                    "messages": [{"role": "user", "content": "hello"}],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200
        assert received[0]["max_tokens"] == serving.DEFAULT_REQUEST_OUTPUT_TOKENS
    finally:
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()


def test_final_review_sampling_flags_are_an_allowlist() -> None:
    revision = "a" * 40
    for flags in (
        "-c 1",
        "-np 8",
        "-ngl 999",
        "-ctk f16",
        "--split-mode row",
        "--no-kv-offload",
        "--batch-size 999999",
    ):
        with pytest.raises(ServingError, match="sampling|managed runtime"):
            serving.parse_manifest(
                f"chat | example/chat | *.gguf | fast | 8192 | {flags} | {revision}\n"
            )


def test_final_review_opener_disables_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[object] = []
    real_build_opener = urllib.request.build_opener

    def capture_handlers(*values: object) -> urllib.request.OpenerDirector:
        handlers.extend(values)
        return real_build_opener(*values)

    monkeypatch.setattr(urllib.request, "build_opener", capture_handlers)
    opener = serving._no_redirect_opener()
    proxy_handlers = [
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert proxy_handlers == []
    assert isinstance(handlers[0], urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}


def test_final_review_shared_placements_reject_aggregate_ram_or_vram_oom() -> None:
    resources = ResourceSnapshot(
        system_total_mib=24 * GIB,
        system_available_mib=22 * GIB,
        backend=Backend.CUDA,
        accelerator_total_mib=12 * GIB,
        accelerator_available_mib=11 * GIB,
        accelerator_shared=False,
        capability_source="fixture",
    )
    with pytest.raises(ServingError, match="aggregate|resident|RAM|VRAM"):
        serving.plan_shared_model_placements(
            model_names=("fast", "embed", "big"),
            resident_names=("fast", "embed"),
            model_memory_mib={
                "fast": 10 * GIB,
                "embed": 8 * GIB,
                "big": 12 * GIB,
            },
            layer_mib={
                "fast": (512,) * 20,
                "embed": (512,) * 16,
                "big": (512,) * 24,
            },
            kv_runtime_mib={"fast": GIB, "embed": GIB, "big": GIB},
            resources=resources,
            requested_backend="cuda",
        )


def test_final_review_binary_tree_provenance_includes_runtime_dlls(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("runtime/bin/llama-server.exe", b"server")
        bundle.writestr("runtime/bin/ggml-vulkan.dll", b"trusted dll")
    installed = tmp_path / "native"
    put(installed, "llama-server.exe", b"server")
    sidecar = put(installed, "ggml-vulkan.dll", b"trusted dll")

    serving.validate_binary_tree_against_archive(
        installed_directory=installed,
        archive=archive,
        anchor_member="llama-server.exe",
    )
    sidecar.write_bytes(b"changed dll")
    with pytest.raises(ServingError, match="tree|provenance|archive"):
        serving.validate_binary_tree_against_archive(
            installed_directory=installed,
            archive=archive,
            anchor_member="llama-server.exe",
        )


def test_final_review_headless_engine_probe_does_not_call_syncing_launchers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    put(tmp_path, "state/generated/claude-code/settings.json", "{}\n")
    put(tmp_path, "state/generated/opencode/opencode.json", "{}\n")
    put(tmp_path, ".tools/npm/claude.cmd", "@exit /b 0\n")
    put(tmp_path, ".tools/npm/opencode.cmd", "@exit /b 0\n")
    calls: list[list[str]] = []
    sanitized: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        if Path(args[0]).stem == "claude":
            settings_path = Path(args[args.index("--settings") + 1])
            sanitized["claude"] = json.loads(
                settings_path.read_text(encoding="utf-8")
            )
            assert args[args.index("--tools") + 1] == ""
            assert Path(str(environment["HOME"])).name == "home"
            assert Path(str(environment["HOME"])).is_dir()
        else:
            sanitized["opencode"] = json.loads(
                Path(str(environment["OPENCODE_CONFIG"])).read_text(
                    encoding="utf-8"
                )
            )
        return subprocess.CompletedProcess(args, 0, "ENGINE-OK")

    monkeypatch.setattr(serving.subprocess, "run", fake_run)
    runner = serving._headless_engine_runner(tmp_path, "chat")
    assert runner("claude")[0] == 0
    assert runner("opencode")[0] == 0
    assert all("launch.ps1" not in " ".join(call) for call in calls)
    assert all("launch.sh" not in " ".join(call) for call in calls)
    claude_permissions = sanitized["claude"]["permissions"]  # type: ignore[index]
    assert claude_permissions["allow"] == []
    assert claude_permissions["defaultMode"] == "default"
    assert {"Bash", "Edit", "Read", "Write"}.issubset(
        claude_permissions["deny"]
    )
    assert sanitized["opencode"]["permission"]["bash"] == "deny"  # type: ignore[index]


def test_final_review_doctors_fail_concrete_runtime_probe_failures() -> None:
    windows = (REPO_ROOT / "bootstrap/doctor.ps1").read_text(encoding="utf-8")
    macos = (REPO_ROOT / "bootstrap/doctor.sh").read_text(encoding="utf-8")
    assert re.search(r'LoadedProbe.*status -eq "FAIL".*BAD', windows, re.DOTALL)
    assert re.search(r'loaded_status.*FAIL.*bad', macos, re.DOTALL)
    assert re.search(r'VerifyExit.*else.*BAD', windows, re.DOTALL)
    assert re.search(r'verify_exit.*else.*bad', macos, re.DOTALL)
    assert "wired_memory_required_mib" in macos
    assert "458752" not in macos
    assert "$NativeDir = Join-Path $Tools \"llama\"" in (
        REPO_ROOT / "serving/serve-windows.ps1"
    ).read_text(encoding="utf-8")


def test_final_review_verifier_exercises_every_admitted_chat_context() -> None:
    requested_models: list[str] = []
    running_calls = 0
    last_model = "chat"

    def transport(
        _method: str, path: str, payload: dict[str, object] | None
    ) -> HttpResponse:
        nonlocal running_calls, last_model
        if path == "/health":
            return HttpResponse(200, {"status": "ok"}, {}, 1)
        if path == "/v1/models":
            return HttpResponse(
                200,
                {"data": [{"id": "chat"}, {"id": "coder"}, {"id": "embed"}]},
                {},
                1,
            )
        if path == "/running":
            running_calls += 1
            if running_calls == 1:
                return HttpResponse(200, {"running": []}, {}, 1)
            return HttpResponse(
                200,
                {
                    "running": [
                        {
                            "model": last_model,
                            "state": "ready",
                            "backend": "cpu",
                            "offloaded_layers": 0,
                        }
                    ]
                },
                {},
                1,
            )
        if path == "/v1/embeddings":
            return HttpResponse(
                200, {"data": [{"embedding": [0.0] * 8}]}, {}, 1
            )
        if path == "/v1/messages":
            return HttpResponse(
                200, {"content": [{"type": "text", "text": "ORACLE-OK"}]}, {}, 1
            )
        if path == "/v1/chat/completions" and payload is not None:
            last_model = str(payload["model"])
            requested_models.append(last_model)
            messages = payload.get("messages")
            content = (
                str(messages[0].get("content", ""))
                if isinstance(messages, list)
                and messages
                and isinstance(messages[0], dict)
                else ""
            )
            if content.startswith("oversize-boundary "):
                return HttpResponse(
                    413,
                    {"error": "rejected"},
                    {"X-Oracle-Admission": "rejected"},
                    1,
                )
            if payload.get("tools"):
                return HttpResponse(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {"function": {"name": "oracle_probe"}}
                                    ]
                                }
                            }
                        ]
                    },
                    {},
                    1,
                )
            if payload.get("response_format"):
                return HttpResponse(
                    200,
                    {"choices": [{"message": {"content": '{"ok":true}'}}]},
                    {},
                    1,
                )
            return HttpResponse(
                200,
                {
                    "choices": [{"message": {"content": "ORACLE-OK"}}],
                    "usage": {"prompt_tokens": 25000},
                },
                {},
                1,
            )
        raise AssertionError(path)

    results = serving.run_offline_probes(
        transport=transport,
        contexts={
            "chat": production_plan(),
            "coder": replace(production_plan(), model_name="coder"),
        },
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        planned_placements={
            "chat": {"backend": "cpu", "offloaded_layers": 0},
            "coder": {"backend": "cpu", "offloaded_layers": 0},
        },
        engine_runner=None,
    )

    coverage = next(
        result for result in results if result.name == "model_context_coverage"
    )
    assert coverage.status == PASS
    assert {"chat", "coder"}.issubset(requested_models)
    assert next(
        result for result in results if result.name == "loaded_backend:coder"
    ).status == PASS


def test_final_review_metal_placement_respects_wired_memory_limit() -> None:
    resources = ResourceSnapshot(
        system_total_mib=128 * GIB,
        system_available_mib=120 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=32 * GIB,
        accelerator_available_mib=32 * GIB,
        accelerator_shared=True,
        capability_source="fixture wired limit",
    )
    with pytest.raises(ServingError, match="wired|accelerator"):
        serving.plan_model_placement(
            model_name="chat",
            model_memory_mib=40 * GIB,
            layer_mib=(GIB,) * 40,
            kv_runtime_mib=2 * GIB,
            resources=resources,
            requested_backend="metal",
        )
    fallback = serving.plan_model_placement(
        model_name="chat",
        model_memory_mib=40 * GIB,
        layer_mib=(GIB,) * 40,
        kv_runtime_mib=2 * GIB,
        resources=resources,
        requested_backend="auto",
    )
    assert fallback.backend == Backend.CPU


def test_metal_shared_placement_offloads_all_layers_with_trusted_metadata() -> None:
    layers = (GIB,) * 48
    resources = ResourceSnapshot(
        system_total_mib=192 * GIB,
        system_available_mib=180 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=160 * GIB,
        accelerator_available_mib=160 * GIB,
        accelerator_shared=True,
        capability_source="fixture metal offload",
    )
    for requested in ("auto", "metal"):
        placement = serving.plan_model_placement(
            model_name="chat",
            model_memory_mib=48 * GIB,
            layer_mib=layers,
            kv_runtime_mib=2 * GIB,
            resources=resources,
            requested_backend=requested,
        )
        assert placement.backend == Backend.METAL
        assert placement.offloaded_layers == len(layers)
        assert placement.total_layers == len(layers)
        assert placement.split_mode == "unified"


def test_shared_metal_placements_offload_every_model_to_metal() -> None:
    resources = ResourceSnapshot(
        system_total_mib=256 * GIB,
        system_available_mib=240 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=200 * GIB,
        accelerator_available_mib=200 * GIB,
        accelerator_shared=True,
        capability_source="fixture metal shared",
    )
    plans = serving.plan_shared_model_placements(
        model_names=("chat", "embed"),
        resident_names=("chat", "embed"),
        model_memory_mib={"chat": 30 * GIB, "embed": 4 * GIB},
        layer_mib={"chat": (GIB,) * 48, "embed": (GIB,) * 36},
        kv_runtime_mib={"chat": 2 * GIB, "embed": GIB},
        resources=resources,
        requested_backend="auto",
    )
    assert plans["chat"].backend == Backend.METAL
    assert plans["chat"].offloaded_layers == 48
    assert plans["embed"].backend == Backend.METAL
    assert plans["embed"].offloaded_layers == 36


def test_metal_placement_selects_gpu_on_minimum_kv_and_only_cpu_when_weights_dont_fit() -> None:
    # A 96 GiB wired budget cannot hold a 30B model's FULL 262144-token KV
    # (~64 GiB) beside its ~35 GiB of weights, but it easily holds the weights
    # plus a production-minimum KV. Placement must therefore stay on Metal when
    # handed the minimum-viable KV, and only fall back to CPU when the WEIGHTS
    # themselves overflow the accelerator.
    layers = (768,) * 48
    resources = ResourceSnapshot(
        system_total_mib=256 * GIB,
        system_available_mib=240 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=96 * GIB,
        accelerator_available_mib=96 * GIB,
        accelerator_shared=True,
        capability_source="fixture wired 96 GiB",
    )
    weights_mib = 35 * GIB
    minimum_kv = serving._minimum_viable_kv_runtime_mib(
        slot="fast",
        parallel_slots=serving.RESIDENT_PARALLEL_SLOTS,
        kv_mib_per_token=0.25,
    )
    nominal_kv = math.ceil(262144 * 0.25) + serving.MODEL_RUNTIME_OVERHEAD_MIB
    assert weights_mib + nominal_kv > (96 * GIB - 1024)
    assert weights_mib + minimum_kv < (96 * GIB - 1024)

    for requested in ("auto", "metal"):
        placement = serving.plan_model_placement(
            model_name="chat",
            model_memory_mib=weights_mib,
            layer_mib=layers,
            kv_runtime_mib=minimum_kv,
            resources=resources,
            requested_backend=requested,
        )
        assert placement.backend == Backend.METAL
        assert placement.offloaded_layers == len(layers) == placement.total_layers
        assert placement.split_mode == "unified"

    # Weights that genuinely overflow the wired budget are the ONLY reason to
    # fall back (auto) or fail (explicit) — never an oversized nominal context.
    too_big = serving.plan_model_placement(
        model_name="chat",
        model_memory_mib=120 * GIB,
        layer_mib=layers,
        kv_runtime_mib=minimum_kv,
        resources=resources,
        requested_backend="auto",
    )
    assert too_big.backend == Backend.CPU
    with pytest.raises(ServingError, match="wired|accelerator"):
        serving.plan_model_placement(
            model_name="chat",
            model_memory_mib=120 * GIB,
            layer_mib=layers,
            kv_runtime_mib=minimum_kv,
            resources=resources,
            requested_backend="metal",
        )


def _metal_shrink_fixture(
    root: Path,
) -> tuple[Path, Path]:
    revision = "a" * 40
    put(
        root,
        "serving/profiles.conf",
        "mid | 64 | chat,embed | chat | chat | chat | ~40 GB\n",
    )
    put(
        root,
        "serving/models.manifest",
        f"chat | example/chat | *.gguf | fast | 262144 | --temp 0.7 | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 |  | {revision}\n",
    )
    put(root, "serving/models.profile", "chat\nembed\n")
    put(
        root,
        "serving/tiers.env",
        "OPUS_MODEL=chat\nSONNET_MODEL=chat\nHAIKU_MODEL=chat\n",
    )
    server = put(root, ".tools/bin/llama-server", b"trusted server")
    swap = put(root, ".tools/bin/llama-swap", b"trusted swap")
    return server, swap


def _metal_shrink_snapshots(
    _root: Path,
    _models: dict[str, serving.ModelSpec],
    selected: tuple[str, ...],
) -> dict[str, serving.ModelSnapshot]:
    sizes = {"chat": 33000, "embed": 4000}
    layer_counts = {"chat": 48, "embed": 36}
    return {
        name: serving.ModelSnapshot(
            name=name,
            paths=(Path(f"/models/{name}/{name}.gguf"),),
            size_bytes=sizes[name] * 1024 * 1024,
            authority_digest="b" * 64,
            layer_mib=(700,) * layer_counts[name],
            kv_mib_per_token=0.25,
        )
        for name in selected
    }


def test_metal_runtime_shrinks_context_to_stay_gpu_resident_not_cpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End-to-end regression guard: on a 256 GiB Mac whose DEFAULT wired budget
    # (96 GiB here) cannot hold the 262144-token nominal KV, the runtime must
    # keep the model on Metal by SHRINKING the served context to fit — never
    # silently launch on CPU (which is what hung the 5-minute chat probe and
    # cascaded into the 429 slot-contention failures on real hardware).
    server, swap = _metal_shrink_fixture(tmp_path)
    monkeypatch.setattr(
        serving, "validate_policy_bound_models", _metal_shrink_snapshots
    )
    resources = ResourceSnapshot(
        system_total_mib=256 * GIB,
        system_available_mib=240 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=96 * GIB,
        accelerator_available_mib=96 * GIB,
        accelerator_shared=True,
        capability_source="fixture metal wired default",
    )

    plan = serving.prepare_runtime(
        root=tmp_path,
        server_path=server,
        llama_swap_path=swap,
        platform="posix",
        resources=resources,
        requested_backend="auto",
    )

    # Whole profile stays on the GPU; no CPU fallback.
    assert plan.backend == Backend.METAL
    for name in ("chat", "embed"):
        placement = plan.placements[name]
        assert placement.backend == Backend.METAL
        assert placement.split_mode == "unified"
        assert placement.offloaded_layers == placement.total_layers
        assert placement.offloaded_layers > 0

    chat_context = plan.contexts["chat"]
    # The context shrank below the 262144 nominal ceiling but still clears the
    # production-minimum envelope, and the fast lane now runs a single slot.
    assert chat_context.parallel_slots == 1
    assert chat_context.slot_context_tokens < 262144
    assert chat_context.advertised_context_tokens >= serving.PRODUCTION_ADVERTISED_CONTEXT

    # The launched server reflects the fitted (shrunk) context and full offload.
    parsed = serving.parse_runtime_config(
        plan.rendered.path.read_text(encoding="utf-8")
    )
    argv = shlex.split(parsed["models"]["chat"]["cmd"])
    ngpu = argv[argv.index("--n-gpu-layers") + 1]
    ctx_size = argv[argv.index("--ctx-size") + 1]
    assert ngpu != "0"
    assert ngpu == str(plan.placements["chat"].offloaded_layers)
    assert ctx_size == str(
        chat_context.slot_context_tokens * chat_context.parallel_slots
    )
    assert argv[argv.index("--parallel") + 1] == "1"
    # GPU-resident placement must keep the KV cache on the GPU, not force it to
    # CPU (which would shuttle the cache across the bus every token).
    assert "--no-kv-offload" not in argv

    # Peak GPU memory (weights + fitted KV) stays within the wired budget.
    wired_budget = (
        resources.accelerator_available_mib - resources.accelerator_reserve_mib
    )
    assert chat_context.peak_memory_mib <= wired_budget


def test_metal_runtime_at_131072_nominal_stays_gpu_resident_and_fits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Right-sizing the fast lane from 262144 to a 131072 nominal must keep the
    # whole profile GPU-resident on a large-memory Mac Studio and grant a fast,
    # fitting context — the KV halves, the slot frees quickly, and the served
    # window never exceeds the (now smaller) 131072 ceiling.
    revision = "a" * 40
    put(
        tmp_path,
        "serving/profiles.conf",
        "mid | 64 | chat,embed | chat | chat | chat | ~40 GB\n",
    )
    put(
        tmp_path,
        "serving/models.manifest",
        f"chat | example/chat | *.gguf | fast | 131072 | --temp 0.7 | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 |  | {revision}\n",
    )
    put(tmp_path, "serving/models.profile", "chat\nembed\n")
    put(
        tmp_path,
        "serving/tiers.env",
        "OPUS_MODEL=chat\nSONNET_MODEL=chat\nHAIKU_MODEL=chat\n",
    )
    server = put(tmp_path, ".tools/bin/llama-server", b"trusted server")
    swap = put(tmp_path, ".tools/bin/llama-swap", b"trusted swap")
    monkeypatch.setattr(
        serving, "validate_policy_bound_models", _metal_shrink_snapshots
    )
    # A 256 GiB Mac Studio (mid profile) with a generous wired budget: weights
    # plus the full 131072-token KV fit with room to spare.
    resources = ResourceSnapshot(
        system_total_mib=256 * GIB,
        system_available_mib=240 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=200 * GIB,
        accelerator_available_mib=200 * GIB,
        accelerator_shared=True,
        capability_source="fixture metal wired 200 GiB",
    )

    plan = serving.prepare_runtime(
        root=tmp_path,
        server_path=server,
        llama_swap_path=swap,
        platform="posix",
        resources=resources,
        requested_backend="auto",
    )

    assert plan.backend == Backend.METAL
    for name in ("chat", "embed"):
        placement = plan.placements[name]
        assert placement.backend == Backend.METAL
        assert placement.split_mode == "unified"
        assert placement.offloaded_layers == placement.total_layers
        assert placement.offloaded_layers > 0

    chat_context = plan.contexts["chat"]
    # A single always-resident slot, a context capped at the new 131072 nominal,
    # and a served window that clears the production-minimum envelope.
    assert chat_context.parallel_slots == 1
    assert chat_context.nominal_context_tokens == 131072
    assert chat_context.slot_context_tokens <= 131072
    assert chat_context.advertised_context_tokens >= serving.PRODUCTION_ADVERTISED_CONTEXT
    # On this large budget the served context reaches the full (smaller) nominal.
    assert chat_context.slot_context_tokens == 131072

    # Peak GPU memory (weights + fitted KV) stays within the wired budget.
    wired_budget = (
        resources.accelerator_available_mib - resources.accelerator_reserve_mib
    )
    assert chat_context.peak_memory_mib <= wired_budget


def test_metal_runtime_falls_back_to_cpu_when_weights_exceed_wired_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # If the WEIGHTS themselves cannot fit the accelerator, CPU is the correct
    # (and only) fallback — the shrink-to-fit logic must not force Metal.
    server, swap = _metal_shrink_fixture(tmp_path)

    def tiny_wired_snapshots(
        root: Path,
        models: dict[str, serving.ModelSpec],
        selected: tuple[str, ...],
    ) -> dict[str, serving.ModelSnapshot]:
        # Both resident models are individually too large for the 8 GiB budget.
        base = _metal_shrink_snapshots(root, models, selected)
        return {
            name: replace(snapshot, size_bytes=20000 * 1024 * 1024)
            for name, snapshot in base.items()
        }

    monkeypatch.setattr(
        serving, "validate_policy_bound_models", tiny_wired_snapshots
    )
    resources = ResourceSnapshot(
        system_total_mib=256 * GIB,
        system_available_mib=240 * GIB,
        backend=Backend.METAL,
        accelerator_total_mib=8 * GIB,
        accelerator_available_mib=8 * GIB,
        accelerator_shared=True,
        capability_source="fixture metal starved wired",
    )

    plan = serving.prepare_runtime(
        root=tmp_path,
        server_path=server,
        llama_swap_path=swap,
        platform="posix",
        resources=resources,
        requested_backend="auto",
    )

    assert plan.backend == Backend.CPU
    for name in ("chat", "embed"):
        assert plan.placements[name].backend == Backend.CPU
        assert plan.placements[name].offloaded_layers == 0


def test_offload_evidence_tolerates_metal_output_layer_off_by_one() -> None:
    # Metal accepts the exact block count and the +1 output-layer report.
    assert serving.offload_evidence_matches("metal", 48, 48)
    assert serving.offload_evidence_matches("metal", 48, 49)
    # A CPU-loaded model (zero offload) must never satisfy a Metal plan.
    assert not serving.offload_evidence_matches("metal", 48, 0)
    assert not serving.offload_evidence_matches("metal", 48, 47)
    # CPU semantics stay exact at zero.
    assert serving.offload_evidence_matches("cpu", 0, 0)
    assert not serving.offload_evidence_matches("cpu", 0, 1)
    # Discrete GPUs keep brittle-free exact equality (no output-layer quirk).
    assert serving.offload_evidence_matches("cuda", 32, 32)
    assert not serving.offload_evidence_matches("cuda", 32, 33)
    assert not serving.offload_evidence_matches("cuda", 32, 31)


@pytest.mark.parametrize(
    ("observed_offload", "expected_status"),
    [(48, PASS), (49, PASS), (0, FAIL)],
)
def test_metal_verify_tolerates_output_layer_off_by_one(
    observed_offload: int, expected_status: str
) -> None:
    def transport(
        _method: str, path: str, _payload: dict[str, object] | None
    ) -> HttpResponse:
        if path == "/health":
            return HttpResponse(200, {"status": "ok"}, {}, 1)
        if path == "/v1/models":
            return HttpResponse(
                200, {"data": [{"id": "chat"}, {"id": "embed"}]}, {}, 1
            )
        if path == "/running":
            return HttpResponse(
                200,
                {
                    "running": [
                        {
                            "model": "chat",
                            "state": "ready",
                            "loaded_backend": "metal",
                            "offloaded_layers": observed_offload,
                        }
                    ]
                },
                {},
                1,
            )
        if path == "/v1/embeddings":
            return HttpResponse(200, {"data": [{"embedding": [0.0] * 8}]}, {}, 1)
        if path == "/v1/messages":
            return HttpResponse(
                200, {"content": [{"type": "text", "text": "ORACLE-OK"}]}, {}, 1
            )
        if path == "/v1/chat/completions":
            return HttpResponse(
                200,
                {
                    "choices": [{"message": {"content": "ORACLE-OK"}}],
                    "usage": {"prompt_tokens": 25000},
                },
                {},
                1,
            )
        raise AssertionError(path)

    results = serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        planned_placements={"chat": {"backend": "metal", "offloaded_layers": 48}},
        engine_runner=None,
    )
    probe = next(result for result in results if result.name == "loaded_backend")
    assert probe.status == expected_status


def test_model_authorities_pin_trusted_layer_counts_for_metal_offload() -> None:
    expected_layers = {
        "deepseek-v3.2": 61,
        "kimi-k2-thinking": 61,
        "qwen3-coder-480b": 62,
        "qwen3-coder-30b": 48,
        "qwen3-coder-30b-q4": 48,
        "qwen2.5-coder-7b": 28,
        "qwen3-embedding-4b": 36,
    }
    payload = json.loads(
        (REPO_ROOT / "serving" / "model-authorities.json").read_text(
            encoding="utf-8"
        )
    )
    models = payload["models"]
    assert set(models) == set(expected_layers)
    for name, count in expected_layers.items():
        layer_mib = models[name]["layer_mib"]
        assert isinstance(layer_mib, list)
        assert len(layer_mib) == count
        assert all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            for value in layer_mib
        )
        total_size = sum(int(item["size"]) for item in models[name]["files"])
        model_size_mib = max(1, math.ceil(total_size / (1024 * 1024)))
        assert (
            math.floor(model_size_mib * 0.75)
            <= sum(layer_mib)
            <= math.ceil(model_size_mib * 1.25)
        )


def test_final_review_gateway_forwards_normalized_completion_limit() -> None:
    received: list[dict[str, object]] = []

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            body = b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    gateway = serving.create_admission_server(
        host="127.0.0.1",
        port=0,
        upstream=f"http://127.0.0.1:{upstream.server_port}",
        contexts={"chat": production_plan()},
        evidence={},
    )
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            gateway.server_port,
            timeout=5,
        )
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "model": "chat",
                    "max_completion_tokens": 73,
                    "messages": [{"role": "user", "content": "normalize"}],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200
        assert received == [
            {
                "model": "chat",
                "max_tokens": 73,
                "messages": [{"role": "user", "content": "normalize"}],
            }
        ]
    finally:
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()


def test_final_review_explicit_backend_still_falls_back_to_smaller_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revision = "a" * 40
    put(
        tmp_path,
        "serving/profiles.conf",
        "large | 16 | large,embed | large | large | large | fixture\n"
        "small | 8 | small,embed | small | small | small | fixture\n",
    )
    put(
        tmp_path,
        "serving/models.manifest",
        f"large | example/large | *.gguf | big | 65536 | | {revision}\n"
        f"small | example/small | *.gguf | fast | 32768 | | {revision}\n"
        f"embed | example/embed | *.gguf | embed | 8192 | | {revision}\n",
    )
    model_paths = {
        name: put(tmp_path, f"models/{name}/{name}.gguf", b"x")
        for name in ("large", "small", "embed")
    }
    server = put(tmp_path, ".tools/bin/llama-server", b"server")
    swap = put(tmp_path, ".tools/bin/llama-swap", b"swap")

    def snapshots(
        _root: Path,
        _models: dict[str, serving.ModelSpec],
        selected: tuple[str, ...],
    ) -> dict[str, serving.ModelSnapshot]:
        return {
            name: serving.ModelSnapshot(
                name=name,
                paths=(model_paths[name],),
                size_bytes=1024 * 1024,
                authority_digest="b" * 64,
                layer_mib=(1,),
                kv_mib_per_token=0.001,
            )
            for name in selected
        }

    def placements(**kwargs: object) -> dict[str, serving.PlacementPlan]:
        names = tuple(kwargs["model_names"])  # type: ignore[arg-type]
        if "large" in names:
            raise ServingError("large profile placement exceeds capacity")
        memory = kwargs["model_memory_mib"]
        runtime = kwargs["kv_runtime_mib"]
        assert isinstance(memory, dict)
        assert isinstance(runtime, dict)
        return {
            name: serving.PlacementPlan(
                model_name=name,
                backend=Backend.CPU,
                offloaded_layers=0,
                total_layers=1,
                ram_required_mib=int(memory[name]) + int(runtime[name]),
                vram_required_mib=0,
                kv_runtime_mib=int(runtime[name]),
                split_mode="none",
            )
            for name in names
        }

    monkeypatch.setattr(serving, "validate_policy_bound_models", snapshots)
    monkeypatch.setattr(serving, "plan_shared_model_placements", placements)
    plan = serving.prepare_runtime(
        root=tmp_path,
        server_path=server,
        llama_swap_path=swap,
        platform="windows",
        resources=ResourceSnapshot(
            system_total_mib=32 * GIB,
            system_available_mib=30 * GIB,
            backend=Backend.CPU,
            capability_source="fixture",
        ),
        requested_backend="cpu",
    )

    assert plan.profile.name == "small"
    evidence = json.loads(plan.rendered.metadata_path.read_text(encoding="utf-8"))
    assert evidence["selection"] == {
        "profile_explicit": False,
        "requested_profile": "large",
        "selected_profile": "small",
    }


def test_final_review_warm_start_and_absent_backend_metadata_are_not_false_failures() -> None:
    def transport(
        _method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> HttpResponse:
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
            return HttpResponse(
                200,
                {
                    "running": [
                        {
                            "model": "chat",
                            "state": "ready",
                            "port": 19001,
                        }
                    ]
                },
                {},
                1,
            )
        if path == "/v1/embeddings":
            return HttpResponse(
                200,
                {"data": [{"embedding": [0.0] * 8}]},
                {},
                1,
            )
        if path == "/v1/messages":
            return HttpResponse(
                200,
                {"content": [{"type": "text", "text": "ORACLE-OK"}]},
                {},
                1,
            )
        if path == "/v1/chat/completions" and payload is not None:
            messages = payload.get("messages")
            content = (
                str(messages[0].get("content", ""))
                if isinstance(messages, list)
                and messages
                and isinstance(messages[0], dict)
                else ""
            )
            if content.startswith("oversize-boundary "):
                return HttpResponse(
                    413,
                    {"error": "rejected"},
                    {"X-Oracle-Admission": "rejected"},
                    1,
                )
            if payload.get("tools"):
                return HttpResponse(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {"function": {"name": "oracle_probe"}}
                                    ]
                                }
                            }
                        ]
                    },
                    {},
                    1,
                )
            if payload.get("response_format"):
                return HttpResponse(
                    200,
                    {"choices": [{"message": {"content": '{"ok":true}'}}]},
                    {},
                    1,
                )
            return HttpResponse(
                200,
                {
                    "choices": [{"message": {"content": "ORACLE-OK"}}],
                    "usage": {"prompt_tokens": 25000},
                },
                {},
                1,
            )
        raise AssertionError(path)

    results = serving.run_offline_probes(
        transport=transport,
        contexts={"chat": production_plan()},
        chat_model="chat",
        embedding_model="embed",
        listeners=("127.0.0.1:9099",),
        listener_inspector=lambda port: (
            (f"127.0.0.1:{port}",)
            if port in {9099, 9098, 19001}
            else ()
        ),
        expected_listener_ports={"public": 9099, "internal": 9098},
        planned_placements={
            "chat": {"backend": "cpu", "offloaded_layers": 0}
        },
        engine_runner=None,
    )
    by_name = {result.name: result for result in results}

    assert by_name["cold_warm_state"].status == PASS
    assert "already warm" in by_name["cold_warm_state"].reason
    assert by_name["loaded_backend"].status == PROVISIONAL
