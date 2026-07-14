"""Shared, fail-closed local-serving primitives.

This module is intentionally standard-library only.  Platform wrappers delegate
resource detection, profile selection, rendering, admission, probes, lifecycle
identity checks, and privacy inspection here so Windows and macOS cannot drift.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import math
import os
import platform as host_platform
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, replace
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verification.lifecycle import atomic_write_bytes, atomic_write_text


PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
PROVISIONAL = "PROVISIONAL"


class ServingError(RuntimeError):
    """A serving configuration or admission boundary failed closed."""


class Backend(str, Enum):
    CUDA = "cuda"
    VULKAN = "vulkan"
    METAL = "metal"
    CPU = "cpu"


@dataclass(frozen=True)
class AcceleratorDevice:
    total_mib: int
    free_mib: int
    name: str


@dataclass(frozen=True)
class ResourceSnapshot:
    system_total_mib: int
    system_available_mib: int
    backend: Backend
    accelerator_total_mib: int = 0
    accelerator_available_mib: int = 0
    accelerator_shared: bool = False
    capability_source: str = ""
    os_reserve_mib: int = 0
    runtime_reserve_mib: int = 1024
    accelerator_reserve_mib: int = 1024

    def __post_init__(self) -> None:
        if self.os_reserve_mib == 0:
            object.__setattr__(
                self,
                "os_reserve_mib",
                max(2048, (self.system_total_mib * 8 + 99) // 100),
            )


@dataclass(frozen=True)
class Profile:
    name: str
    minimum_mib: int
    models: tuple[str, ...]
    opus: str
    sonnet: str
    haiku: str
    download: str


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repository: str
    include: str
    slot: str
    nominal_context: int
    flags: tuple[str, ...]
    revision: str


@dataclass(frozen=True)
class ModelSnapshot:
    name: str
    paths: tuple[Path, ...]
    size_bytes: int
    authority_digest: str
    layer_mib: tuple[int, ...] = ()
    kv_mib_per_token: float = 0.25


@dataclass(frozen=True)
class ContextPlan:
    model_name: str
    nominal_context_tokens: int
    parallel_slots: int
    slot_context_tokens: int
    advertised_context_tokens: int
    prompt_tool_overhead_tokens: int
    output_reserve_tokens: int
    model_memory_mib: int
    kv_mib_per_token: float
    peak_memory_mib: int
    usable_memory_mib: int


@dataclass(frozen=True)
class PlacementPlan:
    model_name: str
    backend: Backend
    offloaded_layers: int
    total_layers: int
    ram_required_mib: int
    vram_required_mib: int
    kv_runtime_mib: int
    split_mode: str


@dataclass(frozen=True)
class RequestEstimate:
    prompt_tokens: int
    tool_schema_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.tool_schema_tokens + self.output_tokens


@dataclass(frozen=True)
class RenderedConfig:
    path: Path
    metadata_path: Path


@dataclass(frozen=True)
class RuntimePlan:
    profile: Profile
    resources: ResourceSnapshot
    backend: Backend
    models: Mapping[str, ModelSpec]
    snapshots: Mapping[str, ModelSnapshot]
    contexts: Mapping[str, ContextPlan]
    placements: Mapping[str, PlacementPlan]
    tiers: Mapping[str, str]
    rendered: RenderedConfig


@dataclass(frozen=True)
class PidRecord:
    pid: int
    executable: str
    started_at: float
    command_digest: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any
    headers: Mapping[str, str]
    elapsed_ms: int


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: str
    reason: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class InspectionResult:
    status: str
    reason: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class CuratedSkill:
    name: str
    path: Path
    reason: str


@dataclass(frozen=True)
class SkillCuration:
    allowed: tuple[CuratedSkill, ...]
    flagged: tuple[CuratedSkill, ...]
    excluded: tuple[CuratedSkill, ...]


PORTABLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIER_KEYS = ("OPUS_MODEL", "SONNET_MODEL", "HAIKU_MODEL")
MINIMUM_ADVERTISED_CONTEXT = 8192
PRODUCTION_ADVERTISED_CONTEXT = 52000
DEFAULT_REQUEST_OUTPUT_TOKENS = 1024
CONSERVATIVE_KV_MIB_PER_TOKEN = 0.25
MODEL_RUNTIME_OVERHEAD_MIB = 512
SENSITIVE_KEY = re.compile(
    r"(?:authorization|bearer|cookie|credential|password|secret|"
    r"(?:access[_-]?|refresh[_-]?|auth[_-]?)?token|"
    r"(?:api|auth|client)[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUE = re.compile(
    r"(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{16,}|"
    r"(?:AGENT_MCP_[A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)|"
    r"ANTHROPIC_AUTH_TOKEN|(?:OPENAI|ANTHROPIC)_API_KEY)\s*=\s*[^\s,;]+|"
    r"(?:access[_-]?token|refresh[_-]?token|auth[_-]?key|api[_-]?key)"
    r"\s*[:=]\s*[^\s,;&]+)",
    re.IGNORECASE,
)
SENSITIVE_URL_CREDENTIALS = re.compile(
    r"(https?://)([^/\s:@]+:[^@/\s]+)@",
    re.IGNORECASE,
)


def _data_lines(text: str, label: str) -> Iterable[tuple[int, str]]:
    if text.startswith("\ufeff"):
        raise ServingError(f"{label}: UTF-8 BOM is not supported")
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            yield line_number, raw


def _portable_name(value: str, label: str) -> str:
    if not PORTABLE_NAME.fullmatch(value):
        raise ServingError(f"{label}: invalid portable name {value!r}")
    return value


def parse_profiles(text: str) -> tuple[Profile, ...]:
    """Parse the tracked profile declaration without shell evaluation."""

    profiles: list[Profile] = []
    seen: set[str] = set()
    for line_number, raw in _data_lines(text, "serving/profiles.conf"):
        fields = [field.strip() for field in raw.split("|")]
        if len(fields) != 7:
            raise ServingError(
                f"serving/profiles.conf:{line_number}: expected 7 fields"
            )
        name, minimum, raw_models, opus, sonnet, haiku, download = fields
        _portable_name(name, f"serving/profiles.conf:{line_number}")
        if name in seen:
            raise ServingError(
                f"serving/profiles.conf:{line_number}: duplicate profile {name}"
            )
        seen.add(name)
        try:
            minimum_gib = int(minimum)
        except ValueError as exc:
            raise ServingError(
                f"serving/profiles.conf:{line_number}: minimum memory is not an integer"
            ) from exc
        if minimum_gib <= 0:
            raise ServingError(
                f"serving/profiles.conf:{line_number}: minimum memory must be positive"
            )
        models = tuple(item.strip() for item in raw_models.split(",") if item.strip())
        if not models or len(models) != len(set(models)):
            raise ServingError(
                f"serving/profiles.conf:{line_number}: model list is empty or duplicate"
            )
        for model in (*models, opus, sonnet, haiku):
            _portable_name(model, f"serving/profiles.conf:{line_number}")
        if any(tier not in models for tier in (opus, sonnet, haiku)):
            raise ServingError(
                f"serving/profiles.conf:{line_number}: tier is outside profile models"
            )
        if not download:
            raise ServingError(
                f"serving/profiles.conf:{line_number}: download description is empty"
            )
        profiles.append(
            Profile(
                name=name,
                minimum_mib=minimum_gib * 1024,
                models=models,
                opus=opus,
                sonnet=sonnet,
                haiku=haiku,
                download=download,
            )
        )
    if not profiles:
        raise ServingError("serving/profiles.conf: no profiles declared")
    return tuple(profiles)


def parse_manifest(text: str) -> dict[str, ModelSpec]:
    """Parse the seven-field Task 2 model manifest strictly."""

    models: dict[str, ModelSpec] = {}
    for line_number, raw in _data_lines(text, "serving/models.manifest"):
        fields = [field.strip() for field in raw.split("|")]
        if len(fields) != 7:
            raise ServingError(
                f"serving/models.manifest:{line_number}: expected 7 fields"
            )
        name, repository, include, slot, context, flags, revision = fields
        _portable_name(name, f"serving/models.manifest:{line_number}")
        if name in models:
            raise ServingError(
                f"serving/models.manifest:{line_number}: duplicate model {name}"
            )
        if slot not in {"big", "fast", "embed"}:
            raise ServingError(
                f"serving/models.manifest:{line_number}: unsupported slot {slot!r}"
            )
        if not repository or not include:
            raise ServingError(
                f"serving/models.manifest:{line_number}: repository/include is empty"
            )
        try:
            nominal_context = int(context)
        except ValueError as exc:
            raise ServingError(
                f"serving/models.manifest:{line_number}: context is not an integer"
            ) from exc
        if nominal_context <= 0:
            raise ServingError(
                f"serving/models.manifest:{line_number}: context must be positive"
            )
        if revision != "dynamic" and not COMMIT.fullmatch(revision):
            raise ServingError(
                f"serving/models.manifest:{line_number}: revision is not immutable"
            )
        try:
            parsed_flags = tuple(shlex.split(flags, posix=True))
        except ValueError as exc:
            raise ServingError(
                f"serving/models.manifest:{line_number}: invalid sampling flags"
            ) from exc
        allowed_sampling_flags = {
            "--temp",
            "--temperature",
            "--top-p",
            "--top-k",
            "--min-p",
            "--repeat-penalty",
            "--presence-penalty",
            "--frequency-penalty",
            "--dry-multiplier",
            "--dry-base",
            "--dry-allowed-length",
            "--dry-penalty-last-n",
        }
        unsupported_flags = sorted(
            {
                token.split("=", 1)[0]
                for token in parsed_flags
                if token.startswith("-")
                and token.split("=", 1)[0] not in allowed_sampling_flags
            }
        )
        if unsupported_flags:
            raise ServingError(
                f"serving/models.manifest:{line_number}: only sampling flags are "
                "allowed; managed runtime option(s) refused: "
                + ", ".join(unsupported_flags)
            )
        models[name] = ModelSpec(
            name=name,
            repository=repository,
            include=include,
            slot=slot,
            nominal_context=nominal_context,
            flags=parsed_flags,
            revision=revision,
        )
    if not models:
        raise ServingError("serving/models.manifest: no models declared")
    return models


def parse_tiers(text: str) -> dict[str, str]:
    tiers: dict[str, str] = {}
    for line_number, raw in _data_lines(text, "serving/tiers.env"):
        if "=" not in raw:
            raise ServingError(f"serving/tiers.env:{line_number}: expected KEY=value")
        key, value = (item.strip() for item in raw.split("=", 1))
        if key not in TIER_KEYS:
            raise ServingError(f"serving/tiers.env:{line_number}: unsupported key {key}")
        if key in tiers:
            raise ServingError(f"serving/tiers.env:{line_number}: duplicate key {key}")
        _portable_name(value, f"serving/tiers.env:{line_number}")
        tiers[key] = value
    missing = sorted(set(TIER_KEYS) - set(tiers))
    if missing:
        raise ServingError("serving/tiers.env: missing " + ", ".join(missing))
    return tiers


def parse_active_models(text: str) -> tuple[str, ...]:
    models: list[str] = []
    seen: set[str] = set()
    for line_number, raw in _data_lines(text, "serving/models.profile"):
        name = raw.strip()
        _portable_name(name, f"serving/models.profile:{line_number}")
        if name in seen:
            raise ServingError(
                f"serving/models.profile:{line_number}: duplicate model {name}"
            )
        seen.add(name)
        models.append(name)
    if not models:
        raise ServingError("serving/models.profile: no active models")
    return tuple(models)


def validate_profile_references(
    profiles: Sequence[Profile], models: Mapping[str, ModelSpec]
) -> None:
    for profile in profiles:
        unknown = sorted(set(profile.models) - set(models))
        if unknown:
            raise ServingError(
                f"profile {profile.name} references unknown model(s): "
                + ", ".join(unknown)
            )


def resolve_active_profile(
    profiles: Sequence[Profile],
    models: Mapping[str, ModelSpec],
    active: Sequence[str],
    tiers: Mapping[str, str],
) -> Profile:
    validate_profile_references(profiles, models)
    unknown = sorted(set(active) - set(models))
    if unknown:
        raise ServingError("active profile has unknown model(s): " + ", ".join(unknown))
    matches = [profile for profile in profiles if set(profile.models) == set(active)]
    if not matches:
        raise ServingError("active models do not exactly match a declared profile")
    profile = max(matches, key=lambda item: item.minimum_mib)
    expected = {
        "OPUS_MODEL": profile.opus,
        "SONNET_MODEL": profile.sonnet,
        "HAIKU_MODEL": profile.haiku,
    }
    if dict(tiers) != expected:
        raise ServingError(
            f"tier mapping differs from active profile {profile.name}: "
            f"expected {expected}, found {dict(tiers)}"
        )
    return profile


def usable_capacity_mib(snapshot: ResourceSnapshot) -> int:
    system = max(
        0,
        snapshot.system_available_mib
        - snapshot.os_reserve_mib
        - snapshot.runtime_reserve_mib,
    )
    if snapshot.backend == Backend.CPU or snapshot.accelerator_shared:
        return system
    accelerator = max(
        0,
        snapshot.accelerator_available_mib - snapshot.accelerator_reserve_mib,
    )
    return system + accelerator


def select_profile(
    profiles: Sequence[Profile], snapshot: ResourceSnapshot
) -> Profile:
    capacity = usable_capacity_mib(snapshot)
    eligible = [profile for profile in profiles if profile.minimum_mib <= capacity]
    if not eligible:
        raise ServingError(
            f"no profile fits {capacity / 1024:.1f} GiB usable capacity after reserves"
        )
    return max(eligible, key=lambda item: item.minimum_mib)


def parse_nvidia_smi(text: str) -> tuple[AcceleratorDevice, ...]:
    devices: list[AcceleratorDevice] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        fields = [field.strip() for field in raw.split(",")]
        if len(fields) < 2:
            raise ServingError(f"nvidia-smi line {line_number}: expected total, free")

        def exact_mib(value: str) -> int:
            normalized = re.sub(r"\s*MiB\s*$", "", value, flags=re.IGNORECASE)
            if not re.fullmatch(r"[0-9]+", normalized):
                raise ServingError(
                    f"nvidia-smi line {line_number}: memory is not exact MiB"
                )
            return int(normalized)

        total = exact_mib(fields[0])
        free = exact_mib(fields[1])
        if total <= 0 or free < 0 or free > total:
            raise ServingError(f"nvidia-smi line {line_number}: impossible memory values")
        name = ",".join(fields[2:]).strip() or f"cuda:{len(devices)}"
        devices.append(AcceleratorDevice(total, free, name))
    if not devices:
        raise ServingError("nvidia-smi returned no usable device memory")
    return tuple(devices)


def aggregate_nvidia_memory(
    devices: Sequence[AcceleratorDevice],
) -> tuple[int, int]:
    if not devices:
        raise ServingError("no NVIDIA devices to aggregate")
    return (
        sum(device.total_mib for device in devices),
        sum(device.free_mib for device in devices),
    )


def windows_resource_snapshot(
    *,
    total_mib: int,
    available_mib: int,
    nvidia_smi_output: str | None,
    vulkan_available: bool,
    adapter_ram_bytes: int | None = None,
) -> ResourceSnapshot:
    if total_mib <= 0 or available_mib <= 0 or available_mib > total_mib:
        raise ServingError("Windows system memory evidence is invalid")
    if nvidia_smi_output:
        devices = parse_nvidia_smi(nvidia_smi_output)
        total, free = aggregate_nvidia_memory(devices)
        return ResourceSnapshot(
            system_total_mib=total_mib,
            system_available_mib=available_mib,
            backend=Backend.CUDA,
            accelerator_total_mib=total,
            accelerator_available_mib=free,
            accelerator_shared=False,
            capability_source="nvidia-smi exact MiB",
        )
    ignored = (
        f"; Win32_VideoController.AdapterRAM ignored ({adapter_ram_bytes} bytes)"
        if adapter_ram_bytes is not None
        else "; Win32_VideoController.AdapterRAM ignored"
    )
    return ResourceSnapshot(
        system_total_mib=total_mib,
        system_available_mib=available_mib,
        backend=Backend.VULKAN if vulkan_available else Backend.CPU,
        capability_source=(
            ("Vulkan capability; accelerator memory untrusted" if vulkan_available else "CPU")
            + ignored
        ),
    )


def select_backend(requested: str, *, available: set[Backend]) -> Backend:
    try:
        selected = Backend(requested.lower())
    except ValueError as exc:
        raise ServingError(f"unsupported backend {requested!r}") from exc
    if selected not in available:
        raise ServingError(f"requested backend {selected.value} is unavailable")
    return selected


def backend_evidence(backend: Backend, source: str) -> dict[str, Any]:
    return {
        "selected_backend": backend.value,
        "capability_source": source,
        "loaded_backend": None,
        "offloaded_layers": None,
    }


def with_loaded_backend(
    evidence: Mapping[str, Any], loaded_backend: str, offloaded_layers: int
) -> dict[str, Any]:
    lowered = loaded_backend.lower()
    normalized = next(
        (backend.value for backend in Backend if backend.value in lowered),
        lowered,
    )
    updated = dict(evidence)
    updated["loaded_backend"] = normalized
    updated["offloaded_layers"] = offloaded_layers
    return updated


def offload_evidence_matches(
    expected_backend: str,
    expected_offload: int | None,
    observed_offload: int | None,
) -> bool:
    """Decide whether observed offload evidence satisfies the placement plan.

    CPU placement must report zero offloaded layers. Discrete GPUs report the
    exact planned layer count. Apple Silicon (Metal, unified memory) offloads
    every transformer block, but llama.cpp may additionally count the
    non-repeating output layer, reporting block_count or block_count + 1. We
    therefore accept any fully-offloaded report (observed >= expected) on Metal
    rather than brittle exact equality that would fail on a real Mac.
    """

    if expected_offload is None or observed_offload is None:
        return False
    if expected_backend == Backend.CPU.value:
        return observed_offload == 0
    if expected_backend == Backend.METAL.value:
        return expected_offload > 0 and observed_offload >= expected_offload
    return expected_offload > 0 and observed_offload == expected_offload


def plan_model_placement(
    *,
    model_name: str,
    model_memory_mib: int,
    layer_mib: Sequence[int] | None,
    kv_runtime_mib: int,
    resources: ResourceSnapshot,
    requested_backend: str,
) -> PlacementPlan:
    """Plan model placement without treating RAM and VRAM as fungible."""

    if model_memory_mib <= 0 or kv_runtime_mib <= 0:
        raise ServingError(f"{model_name}: placement memory inputs must be positive")
    ram_available = max(
        0,
        resources.system_available_mib
        - resources.os_reserve_mib
        - resources.runtime_reserve_mib,
    )
    requested = requested_backend.lower()
    if requested not in {"auto", *(backend.value for backend in Backend)}:
        raise ServingError(f"{model_name}: unsupported placement backend {requested!r}")
    desired = resources.backend if requested == "auto" else Backend(requested)

    def cpu_plan() -> PlacementPlan:
        required = model_memory_mib + kv_runtime_mib
        if required > ram_available:
            raise ServingError(
                f"{model_name}: CPU placement needs {required} MiB RAM but "
                f"only {ram_available} MiB is available"
            )
        return PlacementPlan(
            model_name=model_name,
            backend=Backend.CPU,
            offloaded_layers=0,
            total_layers=len(layer_mib or ()),
            ram_required_mib=required,
            vram_required_mib=0,
            kv_runtime_mib=kv_runtime_mib,
            split_mode="none",
        )

    if desired == Backend.CPU:
        return cpu_plan()
    if resources.accelerator_shared:
        # Unified memory is already accounted for in system RAM. A finite layer
        # count and wired-memory ceiling are still required for Metal offload.
        if not layer_mib:
            if requested == "auto":
                return cpu_plan()
            raise ServingError(
                f"{model_name}: {desired.value} placement lacks trusted layer metadata"
            )
        required = model_memory_mib + kv_runtime_mib
        wired_available = max(
            0,
            resources.accelerator_available_mib
            - resources.accelerator_reserve_mib,
        )
        if required > ram_available or model_memory_mib > wired_available:
            if requested == "auto":
                return cpu_plan()
            raise ServingError(
                f"{model_name}: unified placement needs {required} MiB RAM "
                f"and {model_memory_mib} MiB wired accelerator memory; "
                f"available RAM/wired memory is {ram_available}/{wired_available} MiB"
            )
        return PlacementPlan(
            model_name=model_name,
            backend=desired,
            offloaded_layers=len(layer_mib),
            total_layers=len(layer_mib),
            ram_required_mib=required,
            vram_required_mib=model_memory_mib,
            kv_runtime_mib=kv_runtime_mib,
            split_mode="unified",
        )
    vram_available = max(
        0,
        resources.accelerator_available_mib - resources.accelerator_reserve_mib,
    )
    if vram_available <= 0 or not layer_mib:
        if requested == "auto":
            return cpu_plan()
        raise ServingError(
            f"{model_name}: {desired.value} placement has no trusted VRAM/layer evidence"
        )
    normalized_layers = tuple(int(value) for value in layer_mib)
    if any(value <= 0 for value in normalized_layers):
        raise ServingError(f"{model_name}: placement layer memory is invalid")
    offloaded = 0
    vram_required = 0
    for layer in normalized_layers:
        if vram_required + layer > vram_available:
            break
        vram_required += layer
        offloaded += 1
    if offloaded <= 0:
        if requested == "auto":
            return cpu_plan()
        raise ServingError(f"{model_name}: no model layer fits trusted VRAM")
    offloaded_memory = sum(normalized_layers[:offloaded])
    ram_required = max(0, model_memory_mib - offloaded_memory) + kv_runtime_mib
    if ram_required > ram_available:
        if requested == "auto":
            return cpu_plan()
        raise ServingError(
            f"{model_name}: placement needs {ram_required} MiB RAM and "
            f"{vram_required} MiB VRAM; available RAM is {ram_available} MiB"
        )
    return PlacementPlan(
        model_name=model_name,
        backend=desired,
        offloaded_layers=offloaded,
        total_layers=len(normalized_layers),
        ram_required_mib=ram_required,
        vram_required_mib=vram_required,
        kv_runtime_mib=kv_runtime_mib,
        split_mode="layer",
    )


def plan_shared_model_placements(
    *,
    model_names: Sequence[str],
    resident_names: Sequence[str],
    model_memory_mib: Mapping[str, int],
    layer_mib: Mapping[str, Sequence[int] | None],
    kv_runtime_mib: Mapping[str, int],
    resources: ResourceSnapshot,
    requested_backend: str,
) -> dict[str, PlacementPlan]:
    """Place resident models plus any one swappable model in physical pools."""

    names = tuple(model_names)
    residents = tuple(resident_names)
    resident_set = set(residents)
    if (
        not names
        or len(names) != len(set(names))
        or not resident_set.issubset(names)
        or any(
            set(mapping) != set(names)
            for mapping in (model_memory_mib, layer_mib, kv_runtime_mib)
        )
    ):
        raise ServingError("shared placement model inputs are inconsistent")
    swappable = tuple(name for name in names if name not in resident_set)
    requested = requested_backend.lower()
    if requested not in {"auto", *(item.value for item in Backend)}:
        raise ServingError(
            f"unsupported shared placement backend {requested_backend!r}"
        )

    def aggregate(plans: Mapping[str, PlacementPlan]) -> None:
        resident_ram = sum(plans[name].ram_required_mib for name in residents)
        resident_vram = sum(plans[name].vram_required_mib for name in residents)
        swap_ram = max(
            (plans[name].ram_required_mib for name in swappable),
            default=0,
        )
        swap_vram = max(
            (plans[name].vram_required_mib for name in swappable),
            default=0,
        )
        ram_available = max(
            0,
            resources.system_available_mib
            - resources.os_reserve_mib
            - resources.runtime_reserve_mib,
        )
        vram_available = max(
            0,
            resources.accelerator_available_mib
            - resources.accelerator_reserve_mib,
        )
        if resident_ram + swap_ram > ram_available:
            raise ServingError(
                "aggregate resident and swappable placement needs "
                f"{resident_ram + swap_ram} MiB RAM but only "
                f"{ram_available} MiB is available"
            )
        if resident_vram + swap_vram > vram_available:
            raise ServingError(
                "aggregate resident and swappable placement needs "
                f"{resident_vram + swap_vram} MiB VRAM but only "
                f"{vram_available} MiB is available"
            )

    def place(backend: Backend) -> dict[str, PlacementPlan]:
        plans: dict[str, PlacementPlan] = {}
        backend_request = backend.value
        if backend == Backend.CPU or resources.accelerator_shared:
            for name in names:
                plans[name] = plan_model_placement(
                    model_name=name,
                    model_memory_mib=model_memory_mib[name],
                    layer_mib=layer_mib[name],
                    kv_runtime_mib=kv_runtime_mib[name],
                    resources=replace(resources, backend=backend),
                    requested_backend=backend_request,
                )
        else:
            total_budget = max(
                0,
                resources.accelerator_available_mib
                - resources.accelerator_reserve_mib,
            )
            remaining = total_budget
            for name in residents:
                candidate_resources = replace(
                    resources,
                    backend=backend,
                    accelerator_available_mib=(
                        remaining + resources.accelerator_reserve_mib
                    ),
                )
                plans[name] = plan_model_placement(
                    model_name=name,
                    model_memory_mib=model_memory_mib[name],
                    layer_mib=layer_mib[name],
                    kv_runtime_mib=kv_runtime_mib[name],
                    resources=candidate_resources,
                    requested_backend=backend_request,
                )
                remaining -= plans[name].vram_required_mib
            for name in swappable:
                candidate_resources = replace(
                    resources,
                    backend=backend,
                    accelerator_available_mib=(
                        remaining + resources.accelerator_reserve_mib
                    ),
                )
                plans[name] = plan_model_placement(
                    model_name=name,
                    model_memory_mib=model_memory_mib[name],
                    layer_mib=layer_mib[name],
                    kv_runtime_mib=kv_runtime_mib[name],
                    resources=candidate_resources,
                    requested_backend=backend_request,
                )
        aggregate(plans)
        return plans

    backend = resources.backend if requested == "auto" else Backend(requested)
    try:
        return place(backend)
    except ServingError:
        if requested != "auto" or backend == Backend.CPU:
            raise
    return place(Backend.CPU)


def _safe_relative_model_path(value: str) -> Path:
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ServingError(f"unsafe model authority path: {value!r}")
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ServingError(f"unsafe model authority path: {value!r}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _guard_no_link(path: Path, root: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise ServingError(f"model path is a symbolic link: {current}")
        if current == root:
            break
        if root not in current.parents:
            raise ServingError(f"model path escapes its root: {path}")
        current = current.parent


def validate_policy_bound_models(
    root: Path,
    models: Mapping[str, ModelSpec],
    selected: Sequence[str],
) -> dict[str, ModelSnapshot]:
    """Validate selected local shards against Task 2's tracked authority."""

    authority_path = root / "serving" / "model-authorities.json"
    try:
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServingError(f"model authority policy is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ServingError("model authority policy has an unsupported schema")
    raw_authorities = payload.get("models")
    if not isinstance(raw_authorities, dict):
        raise ServingError("model authority policy models must be an object")
    snapshots: dict[str, ModelSnapshot] = {}
    if len(selected) != len(set(selected)):
        raise ServingError("selected model list contains duplicates")
    for name in selected:
        model = models.get(name)
        if model is None:
            raise ServingError(f"selected model is unsupported: {name}")
        authority = raw_authorities.get(name)
        if not isinstance(authority, dict):
            raise ServingError(f"selected model has no policy-bound authority: {name}")
        if authority.get("repository") != model.repository:
            raise ServingError(f"{name}: authority repository mismatch")
        revision = authority.get("revision")
        if not isinstance(revision, str) or not COMMIT.fullmatch(revision):
            raise ServingError(f"{name}: authority revision is not immutable")
        if model.revision != "dynamic" and model.revision != revision:
            raise ServingError(f"{name}: authority revision mismatch")
        if authority.get("include") != model.include:
            raise ServingError(f"{name}: authority include pattern mismatch")
        files = authority.get("files")
        if not isinstance(files, list) or not files:
            raise ServingError(f"{name}: authority has no model shards")
        model_root = root / "models" / name
        _guard_no_link(model_root, root)
        paths: list[Path] = []
        total_size = 0
        seen: set[str] = set()
        for raw_file in files:
            if not isinstance(raw_file, dict):
                raise ServingError(f"{name}: authority shard is not an object")
            relative_text = str(raw_file.get("path", ""))
            relative = _safe_relative_model_path(relative_text)
            expected_digest = str(raw_file.get("sha256", ""))
            expected_size = raw_file.get("size")
            if (
                relative_text in seen
                or not fnmatch.fnmatchcase(relative.as_posix(), model.include)
                or not SHA256.fullmatch(expected_digest)
                or not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
            ):
                raise ServingError(f"{name}: invalid authority shard {relative_text!r}")
            seen.add(relative_text)
            path = model_root / relative
            _guard_no_link(path, root)
            if not path.is_file():
                raise ServingError(f"{name}: policy-bound model shard is missing: {relative}")
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                raise ServingError(f"{name}: model size mismatch: {relative}")
            if _sha256_file(path) != expected_digest:
                raise ServingError(f"{name}: model digest mismatch: {relative}")
            paths.append(path)
            total_size += actual_size
        authority_digest = hashlib.sha256(
            (
                json.dumps(authority, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        ).hexdigest()
        raw_layers = authority.get("layer_mib", [])
        if not isinstance(raw_layers, list) or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in raw_layers
        ):
            raise ServingError(f"{name}: authority layer memory is invalid")
        declared_layers = tuple(raw_layers)
        model_size_mib = max(1, math.ceil(total_size / (1024 * 1024)))
        if declared_layers and not (
            math.floor(model_size_mib * 0.75)
            <= sum(declared_layers)
            <= math.ceil(model_size_mib * 1.25)
        ):
            raise ServingError(
                f"{name}: authority layer memory does not conservatively "
                "represent model size"
            )
        if declared_layers:
            conservative_model_mib = math.ceil(model_size_mib * 1.08) + 256
            scale = max(
                1.0,
                conservative_model_mib / sum(declared_layers),
            )
            layer_mib = tuple(
                max(1, math.ceil(value * scale))
                for value in declared_layers
            )
        else:
            layer_mib = ()
        raw_kv_mib = authority.get(
            "kv_mib_per_token",
            CONSERVATIVE_KV_MIB_PER_TOKEN,
        )
        if (
            not isinstance(raw_kv_mib, (int, float))
            or isinstance(raw_kv_mib, bool)
            or not math.isfinite(float(raw_kv_mib))
            or not 0.001 <= float(raw_kv_mib) <= 8.0
        ):
            raise ServingError(
                f"{name}: authority KV memory metadata is invalid"
            )
        snapshots[name] = ModelSnapshot(
            name=name,
            paths=tuple(paths),
            size_bytes=total_size,
            authority_digest=authority_digest,
            layer_mib=layer_mib,
            kv_mib_per_token=float(raw_kv_mib),
        )
    return snapshots


def plan_context(
    *,
    model_name: str,
    nominal_context_tokens: int,
    requested_parallel: int,
    model_memory_mib: int,
    usable_memory_mib: int,
    prompt_tool_overhead_tokens: int,
    output_reserve_tokens: int,
    kv_mib_per_token: float,
    minimum_advertised_context: int = MINIMUM_ADVERTISED_CONTEXT,
) -> ContextPlan:
    """Produce a context/concurrency envelope that fits one admitted request."""

    integer_values = (
        nominal_context_tokens,
        requested_parallel,
        model_memory_mib,
        usable_memory_mib,
        prompt_tool_overhead_tokens,
        output_reserve_tokens,
    )
    if (
        any(value <= 0 for value in integer_values)
        or kv_mib_per_token <= 0
        or minimum_advertised_context <= 0
    ):
        raise ServingError("context admission inputs must be positive")
    available_for_kv = usable_memory_mib - model_memory_mib - MODEL_RUNTIME_OVERHEAD_MIB
    if available_for_kv <= 0:
        raise ServingError(f"{model_name}: model leaves no memory for context")
    overhead = prompt_tool_overhead_tokens + output_reserve_tokens
    for slots in range(requested_parallel, 0, -1):
        nominal_per_slot = nominal_context_tokens // slots
        memory_per_slot = int(available_for_kv // (kv_mib_per_token * slots))
        slot_context = min(nominal_per_slot, memory_per_slot)
        advertised = slot_context - overhead
        if advertised < minimum_advertised_context:
            continue
        peak = (
            model_memory_mib
            + MODEL_RUNTIME_OVERHEAD_MIB
            + math.ceil(slot_context * slots * kv_mib_per_token)
        )
        if peak <= usable_memory_mib:
            return ContextPlan(
                model_name=model_name,
                nominal_context_tokens=nominal_context_tokens,
                parallel_slots=slots,
                slot_context_tokens=slot_context,
                advertised_context_tokens=advertised,
                prompt_tool_overhead_tokens=prompt_tool_overhead_tokens,
                output_reserve_tokens=output_reserve_tokens,
                model_memory_mib=model_memory_mib,
                kv_mib_per_token=kv_mib_per_token,
                peak_memory_mib=peak,
                usable_memory_mib=usable_memory_mib,
            )
    raise ServingError(
        f"{model_name}: no safe context/concurrency envelope fits available memory"
    )


def _estimate_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    try:
        serialized = (
            value
            if isinstance(value, str)
            else json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ServingError(
            "request contains binary or unsupported non-JSON content"
        ) from exc
    # Byte-level tokenizers cannot emit more ordinary tokens than input bytes.
    # Counting every UTF-8 byte is intentionally conservative for code,
    # punctuation, emoji, and tokenizer variants.
    return len(serialized.encode("utf-8"))


def estimate_request_tokens(payload: Mapping[str, Any]) -> RequestEstimate:
    if not isinstance(payload, Mapping):
        raise ServingError("request payload must be a JSON object")
    unsupported_limits = sorted(
        field for field in ("n_predict", "max_output_tokens") if field in payload
    )
    if unsupported_limits:
        raise ServingError(
            "request uses unsupported output limit field(s): "
            + ", ".join(unsupported_limits)
        )
    choices = payload.get("n", 1)
    if (
        not isinstance(choices, int)
        or isinstance(choices, bool)
        or choices != 1
    ):
        raise ServingError("request completion count must be exactly one")
    output_fields = [
        field
        for field in ("max_tokens", "max_completion_tokens")
        if field in payload
    ]
    if len(output_fields) > 1:
        raise ServingError("request has conflicting output limit fields")
    output_value = (
        payload[output_fields[0]]
        if output_fields
        else (
            0
            if "input" in payload and "messages" not in payload
            else DEFAULT_REQUEST_OUTPUT_TOKENS
        )
    )
    if not isinstance(output_value, int) or isinstance(output_value, bool):
        raise ServingError("request max_tokens is invalid")
    output = output_value
    embedding_request = (
        "input" in payload
        and "messages" not in payload
        and not output_fields
    )
    if output < 0 or (output == 0 and not embedding_request):
        raise ServingError("request max_tokens is invalid")
    try:
        canonical = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ServingError(
            "request contains binary or unsupported non-JSON content"
        ) from exc
    prompt = len(canonical.encode("utf-8"))
    tool_fields = {
        field: payload[field]
        for field in (
            "tools",
            "tool_choice",
            "response_format",
            "grammar",
            "json_schema",
        )
        if field in payload
    }
    tool_tokens = _estimate_text_tokens(tool_fields) if tool_fields else 0
    return RequestEstimate(prompt, tool_tokens, output)


def dispatch_admitted(
    plan: ContextPlan,
    payload: Mapping[str, Any],
    *,
    active_requests: int,
    invoke: Callable[[Mapping[str, Any]], Any],
) -> Any:
    if active_requests < 0:
        raise ServingError("active request count is invalid")
    if active_requests >= plan.parallel_slots:
        raise ServingError(
            f"{plan.model_name}: unsafe contention; {plan.parallel_slots} slot(s) admitted"
        )
    estimate = estimate_request_tokens(payload)
    required = (
        estimate.total_tokens
        + plan.prompt_tool_overhead_tokens
        + plan.output_reserve_tokens
    )
    if required > plan.slot_context_tokens:
        raise ServingError(
            f"{plan.model_name}: request context {required} exceeds admitted "
            f"slot context {plan.slot_context_tokens}"
        )
    return invoke(payload)


def require_loopback(value: str) -> str:
    lowered = value.strip().lower()
    if lowered == "localhost":
        return value
    candidate = lowered.strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ServingError(f"runtime bind must be loopback, found {value!r}") from exc
    if not address.is_loopback:
        raise ServingError(f"runtime bind must be loopback, found {value!r}")
    return value


def _quote_argv(argv: Sequence[str], platform: str) -> str:
    if platform == "posix":
        return shlex.join(argv)
    if platform == "windows":
        return subprocess.list2cmdline(list(argv))
    raise ServingError(f"unsupported command platform: {platform}")


def _runtime_output_path(path: Path) -> Path:
    normalized = path.as_posix().lower()
    if "/state/generated/serving/" not in f"/{normalized.lstrip('/')}":
        raise ServingError("generated serving config must be under state/generated/serving")
    return path


def write_launchd_plist(
    *,
    output: Path,
    label: str,
    python: Path,
    root: Path,
    llama_swap: Path,
    config: Path,
    admission: Path,
    stdout: Path,
    stderr: Path,
) -> None:
    output = _runtime_output_path(output)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{2,127}", label):
        raise ServingError("launchd label is invalid")
    arguments = [
        str(python),
        str(root / "verification" / "serving.py"),
        "service-run",
        "--root",
        str(root),
        "--llama-swap",
        str(llama_swap),
        "--config",
        str(config),
        "--admission",
        str(admission),
        "--gateway-host",
        "127.0.0.1",
        "--gateway-port",
        "9099",
        "--upstream-host",
        "127.0.0.1",
        "--upstream-port",
        "9098",
    ]
    payload = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
    }
    atomic_write_bytes(
        output,
        plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True),
    )


def render_runtime_config(
    *,
    output: Path,
    server_path: Path,
    model_paths: Mapping[str, Sequence[Path]],
    models: Mapping[str, ModelSpec],
    contexts: Mapping[str, ContextPlan],
    backend: Backend,
    platform: str,
    placements: Mapping[str, PlacementPlan] | None = None,
    host: str = "127.0.0.1",
) -> RenderedConfig:
    require_loopback(host)
    output = _runtime_output_path(output)
    if set(model_paths) != set(models) or set(contexts) != set(models):
        raise ServingError("models, paths, and context plans must name the same models")
    lines = [
        "# GENERATED by verification/serving.py; do not edit.",
        "# llama-swap itself is bound to an internal loopback port by the service runner.",
        "healthCheckTimeout: 900",
        "logLevel: info",
        "",
        "models:",
    ]
    fast_alias_used = False
    embed_alias_used = False
    alias_map: dict[str, str] = {}
    resolved_placements: dict[str, PlacementPlan] = {}
    for name in sorted(models):
        model = models[name]
        paths = tuple(model_paths[name])
        if not paths:
            raise ServingError(f"{name}: no policy-bound model path")
        primary = sorted(paths, key=lambda item: item.as_posix())[0]
        context = contexts[name]
        if context.model_name != name:
            raise ServingError(f"{name}: context plan names another model")
        placement = (placements or {}).get(name)
        if placement is None:
            if backend != Backend.CPU:
                raise ServingError(f"{name}: accelerator placement plan is missing")
            placement = PlacementPlan(
                model_name=name,
                backend=Backend.CPU,
                offloaded_layers=0,
                total_layers=0,
                ram_required_mib=context.peak_memory_mib,
                vram_required_mib=0,
                kv_runtime_mib=max(
                    1, context.peak_memory_mib - context.model_memory_mib
                ),
                split_mode="none",
            )
        if placement.model_name != name:
            raise ServingError(f"{name}: placement plan names another model")
        resolved_placements[name] = placement
        argv = [
            str(server_path),
            "--host",
            host,
            "--port",
            "${PORT}",
            "-m",
            str(primary),
            "--ctx-size",
            str(context.slot_context_tokens * context.parallel_slots),
            "--parallel",
            str(context.parallel_slots),
            "--n-gpu-layers",
            str(placement.offloaded_layers),
            "--no-kv-offload",
            "--jinja",
            "--cache-reuse",
            "256",
            "--cache-type-k",
            "q8_0",
            "--cache-type-v",
            "q8_0",
        ]
        if placement.split_mode == "layer":
            argv.extend(["--split-mode", "layer"])
        if model.slot == "embed":
            argv.extend(["--embeddings", "--pooling", "last"])
        argv.extend(model.flags)
        command = _quote_argv(argv, platform)
        lines.extend(
            [
                f'  "{name}":',
                "    cmd: >-",
                f"      {command}",
                "    ttl: 0",
                f"    # oracle-advertised-context: {context.advertised_context_tokens}",
            ]
        )
        if model.slot == "fast" and not fast_alias_used:
            fast_alias_used = True
            alias_map.update({"gpt-4o-mini": name, "gpt-4o": name})
            lines.extend(
                [
                    "    aliases:",
                    "      - gpt-4o-mini",
                    "      - gpt-4o",
                ]
            )
        elif model.slot == "embed" and not embed_alias_used:
            embed_alias_used = True
            alias_map.update(
                {
                    "text-embedding-3-large": name,
                    "text-embedding-3-small": name,
                    "text-embedding-ada-002": name,
                }
            )
            lines.extend(
                [
                    "    aliases:",
                    "      - text-embedding-3-large",
                    "      - text-embedding-3-small",
                    "      - text-embedding-ada-002",
                ]
            )
    big = [name for name, model in models.items() if model.slot == "big"]
    resident = [name for name, model in models.items() if model.slot != "big"]
    if not resident:
        raise ServingError("at least one resident fast/embed model is required")
    lines.extend(["", "groups:"])
    if big:
        lines.extend(["  big:", "    swap: true", "    exclusive: false", "    members:"])
        lines.extend(f'      - "{name}"' for name in sorted(big))
    lines.extend(
        [
            "  resident:",
            "    swap: false",
            "    exclusive: false",
            "    persistent: true",
            "    members:",
        ]
    )
    lines.extend(f'      - "{name}"' for name in sorted(resident))
    metadata = {
        "schema_version": 1,
        "listen": {
            "public": "127.0.0.1:9099",
            "llama_swap_internal": "127.0.0.1:9098",
        },
        "backend": backend_evidence(backend, "configured capability"),
        "aliases": alias_map,
        "models": {
            name: {
                "slot": models[name].slot,
                "nominal_context": models[name].nominal_context,
                "server_context": (
                    contexts[name].slot_context_tokens
                    * contexts[name].parallel_slots
                ),
                "parallel_slots": contexts[name].parallel_slots,
                "slot_context": contexts[name].slot_context_tokens,
                "advertised_context": contexts[name].advertised_context_tokens,
                "prompt_tool_overhead": (
                    contexts[name].prompt_tool_overhead_tokens
                ),
                "output_reserve": contexts[name].output_reserve_tokens,
                "model_memory_mib": contexts[name].model_memory_mib,
                "kv_mib_per_token": contexts[name].kv_mib_per_token,
                "peak_memory_mib": contexts[name].peak_memory_mib,
                "usable_memory_mib": contexts[name].usable_memory_mib,
                "placement": {
                    "backend": resolved_placements[name].backend.value,
                    "offloaded_layers": resolved_placements[name].offloaded_layers,
                    "total_layers": resolved_placements[name].total_layers,
                    "ram_required_mib": resolved_placements[name].ram_required_mib,
                    "vram_required_mib": resolved_placements[name].vram_required_mib,
                    "split_mode": resolved_placements[name].split_mode,
                },
            }
            for name in sorted(models)
        },
    }
    metadata_path = output.with_name("admission.json")
    atomic_write_text(output, "\n".join(lines) + "\n")
    metadata["config_sha256"] = _sha256_file(output)
    atomic_write_text(
        metadata_path,
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )
    return RenderedConfig(output, metadata_path)


def _read_utf8(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ServingError(f"{label} is unreadable: {exc}") from exc


def _profile_tiers(profile: Profile) -> dict[str, str]:
    return {
        "OPUS_MODEL": profile.opus,
        "SONNET_MODEL": profile.sonnet,
        "HAIKU_MODEL": profile.haiku,
    }


def prepare_runtime(
    *,
    root: Path,
    server_path: Path,
    llama_swap_path: Path | None = None,
    platform: str,
    resources: ResourceSnapshot,
    requested_backend: str = "auto",
) -> RuntimePlan:
    """Validate declarations/authority, admit resources, and render runtime state."""

    root = Path(os.path.abspath(os.fspath(root)))
    server_path = Path(os.path.abspath(os.fspath(server_path)))
    if not server_path.is_file():
        raise ServingError(f"llama-server binary is missing: {server_path}")
    if llama_swap_path is not None:
        llama_swap_path = Path(os.path.abspath(os.fspath(llama_swap_path)))
        if not llama_swap_path.is_file():
            raise ServingError(f"llama-swap binary is missing: {llama_swap_path}")
    profiles = parse_profiles(
        _read_utf8(root / "serving" / "profiles.conf", "profile declaration")
    )
    models = parse_manifest(
        _read_utf8(root / "serving" / "models.manifest", "model manifest")
    )
    validate_profile_references(profiles, models)
    active_path = root / "serving" / "models.profile"
    tiers_path = root / "serving" / "tiers.env"
    profile_explicit = active_path.is_file()
    if profile_explicit:
        active = parse_active_models(_read_utf8(active_path, "active model profile"))
        if tiers_path.is_file():
            tiers = parse_tiers(_read_utf8(tiers_path, "tier mapping"))
        else:
            matching = [
                item for item in profiles if set(item.models) == set(active)
            ]
            if not matching:
                raise ServingError(
                    "active models do not exactly match a declared profile"
                )
            tiers = _profile_tiers(max(matching, key=lambda item: item.minimum_mib))
        profile = resolve_active_profile(profiles, models, active, tiers)
    else:
        profile = select_profile(profiles, resources)
        active = profile.models
        tiers = _profile_tiers(profile)
    requested_profile_name = profile.name
    available_backends = {Backend.CPU, resources.backend}
    backend = (
        resources.backend
        if requested_backend.lower() == "auto"
        else select_backend(requested_backend, available=available_backends)
    )
    while True:
        try:
            snapshots = validate_policy_bound_models(root, models, active)
            resident_names = [
                name for name in active if models[name].slot in {"fast", "embed"}
            ]
            if not resident_names:
                raise ServingError("selected profile has no resident model")
            big_names = [name for name in active if models[name].slot == "big"]
            model_memory = {}
            for name in active:
                raw = max(
                    1,
                    math.ceil(snapshots[name].size_bytes / (1024 * 1024)),
                )
                model_memory[name] = math.ceil(raw * 1.08) + 256
            layers = {
                name: snapshots[name].layer_mib or None for name in active
            }
            selected_resources = replace(resources, backend=backend)
            preliminary_placements = plan_shared_model_placements(
                model_names=active,
                resident_names=resident_names,
                model_memory_mib=model_memory,
                layer_mib=layers,
                kv_runtime_mib={
                    name: (
                        math.ceil(
                            models[name].nominal_context
                            * snapshots[name].kv_mib_per_token
                        )
                        + MODEL_RUNTIME_OVERHEAD_MIB
                    )
                    for name in active
                },
                resources=selected_resources,
                requested_backend=requested_backend,
            )
            placement_backends = {
                item.backend for item in preliminary_placements.values()
            }
            if len(placement_backends) != 1:
                raise ServingError(
                    "profile models do not share one safe backend placement"
                )
            backend = next(iter(placement_backends))
            selected_resources = replace(resources, backend=backend)
            usable = usable_capacity_mib(selected_resources)
            if profile.minimum_mib > usable:
                raise ServingError(
                    f"profile {profile.name} requires "
                    f"{profile.minimum_mib / 1024:.1f} GiB but only "
                    f"{usable / 1024:.1f} GiB is usable after reserves"
                )
            resident_memory = sum(model_memory[name] for name in resident_names)
            largest_big = max(
                (model_memory[name] for name in big_names),
                default=0,
            )
            simultaneous_weights = resident_memory + largest_big
            if simultaneous_weights + MODEL_RUNTIME_OVERHEAD_MIB >= usable:
                raise ServingError(
                    f"profile {profile.name} policy-bound weights require "
                    f"{simultaneous_weights} MiB before context, but only "
                    f"{usable} MiB is usable"
                )
            desired = {
                name: models[name].nominal_context
                * (2 if models[name].slot == "fast" else 1)
                for name in active
            }
            desired_total = sum(desired.values())
            context_pool = usable - simultaneous_weights
            contexts = {}
            for name in active:
                model = models[name]
                share = max(
                    1024,
                    int(context_pool * (desired[name] / desired_total)),
                )
                is_embedding = model.slot == "embed"
                contexts[name] = plan_context(
                    model_name=name,
                    nominal_context_tokens=model.nominal_context,
                    requested_parallel=2 if model.slot == "fast" else 1,
                    model_memory_mib=simultaneous_weights,
                    usable_memory_mib=min(
                        usable,
                        simultaneous_weights + share,
                    ),
                    prompt_tool_overhead_tokens=(
                        512 if is_embedding else 4096
                    ),
                    output_reserve_tokens=256 if is_embedding else 4096,
                    kv_mib_per_token=snapshots[name].kv_mib_per_token,
                    minimum_advertised_context=(
                        1024
                        if is_embedding
                        else PRODUCTION_ADVERTISED_CONTEXT
                        if model.nominal_context >= 65536
                        else MINIMUM_ADVERTISED_CONTEXT
                    ),
                )
            placements = plan_shared_model_placements(
                model_names=active,
                resident_names=resident_names,
                model_memory_mib=model_memory,
                layer_mib=layers,
                kv_runtime_mib={
                    name: (
                        math.ceil(
                            contexts[name].slot_context_tokens
                            * contexts[name].parallel_slots
                            * contexts[name].kv_mib_per_token
                        )
                        + MODEL_RUNTIME_OVERHEAD_MIB
                    )
                    for name in active
                },
                resources=selected_resources,
                requested_backend=backend.value,
            )
            break
        except ServingError:
            if profile_explicit:
                raise
            smaller = [
                item
                for item in profiles
                if item.minimum_mib < profile.minimum_mib
            ]
            if not smaller:
                raise
            profile = max(smaller, key=lambda item: item.minimum_mib)
            active = profile.models
            tiers = _profile_tiers(profile)
            backend = resources.backend
    rendered = render_runtime_config(
        output=root / "state" / "generated" / "serving" / "llama-swap.yaml",
        server_path=server_path,
        model_paths={name: snapshots[name].paths for name in active},
        models={name: models[name] for name in active},
        contexts=contexts,
        backend=backend,
        platform=platform,
        placements=placements,
    )
    metadata = json.loads(rendered.metadata_path.read_text(encoding="utf-8"))
    wired_memory_required_mib = 0
    if backend == Backend.METAL:
        wired_memory_required_mib = sum(
            placements[name].vram_required_mib
            for name in resident_names
        ) + max(
            (
                placements[name].vram_required_mib
                for name in active
                if name not in resident_names
            ),
            default=0,
        )
    metadata.update(
        {
            "profile": profile.name,
            "selection": {
                "profile_explicit": profile_explicit,
                "requested_profile": requested_profile_name,
                "selected_profile": profile.name,
            },
            "tiers": tiers,
            "resources": {
                "system_total_mib": selected_resources.system_total_mib,
                "system_available_mib": selected_resources.system_available_mib,
                "os_reserve_mib": selected_resources.os_reserve_mib,
                "runtime_reserve_mib": selected_resources.runtime_reserve_mib,
                "accelerator_total_mib": selected_resources.accelerator_total_mib,
                "accelerator_available_mib": (
                    selected_resources.accelerator_available_mib
                ),
                "accelerator_reserve_mib": (
                    selected_resources.accelerator_reserve_mib
                ),
                "accelerator_shared": selected_resources.accelerator_shared,
                "wired_memory_required_mib": wired_memory_required_mib,
                "usable_capacity_mib": usable,
                "capability_source": selected_resources.capability_source,
            },
            "resident_models": sorted(resident_names),
            "binaries": {
                "llama_server": {
                    "path": str(server_path),
                    "sha256": _sha256_file(server_path),
                },
                "llama_server_tree": (
                    {
                        "path": str(server_path.parent),
                        "sha256": _directory_tree_digest(server_path.parent),
                    }
                    if platform == "windows"
                    else None
                ),
                "llama_swap": (
                    {
                        "path": str(llama_swap_path),
                        "sha256": _sha256_file(llama_swap_path),
                    }
                    if llama_swap_path is not None
                    else None
                ),
            },
        }
    )
    for name in active:
        metadata["models"][name]["authority_digest"] = snapshots[
            name
        ].authority_digest
        metadata["models"][name]["model_bytes"] = snapshots[name].size_bytes
    atomic_write_text(
        rendered.metadata_path,
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )
    generated = rendered.path.parent
    atomic_write_text(
        generated / "tiers.env",
        "".join(f"{key}={tiers[key]}\n" for key in TIER_KEYS),
    )
    atomic_write_text(
        generated / "profile.json",
        json.dumps(
            {
                "schema_version": 1,
                "profile": profile.name,
                "models": list(active),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return RuntimePlan(
        profile=profile,
        resources=selected_resources,
        backend=backend,
        models={name: models[name] for name in active},
        snapshots=snapshots,
        contexts=contexts,
        placements=placements,
        tiers=tiers,
        rendered=rendered,
    )


def validate_binary_against_archive(
    *, installed: Path, archive: Path, member_name: str
) -> None:
    """Verify an installed runtime binary against its policy-bound archive."""

    if not installed.is_file() or not archive.is_file():
        raise ServingError("runtime binary provenance input is missing")
    try:
        with zipfile.ZipFile(archive) as bundle:
            matches = [
                name
                for name in bundle.namelist()
                if not name.endswith("/") and Path(name).name == member_name
            ]
            if len(matches) != 1:
                raise ServingError(
                    f"runtime archive must contain exactly one {member_name}"
                )
            expected = bundle.read(matches[0])
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ServingError("runtime archive provenance is unreadable") from exc
    try:
        observed = installed.read_bytes()
    except OSError as exc:
        raise ServingError("installed runtime binary is unreadable") from exc
    if not expected or not hmac.compare_digest(
        hashlib.sha256(observed).hexdigest(),
        hashlib.sha256(expected).hexdigest(),
    ):
        raise ServingError("installed runtime binary differs from policy archive")


def _directory_tree_digest(directory: Path) -> str:
    if not directory.is_dir() or directory.is_symlink():
        raise ServingError("runtime binary tree is missing or unsafe")
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ServingError("runtime binary tree contains a symbolic link")
        if path.is_file():
            files.append((path.relative_to(directory).as_posix(), path))
        elif not path.is_dir():
            raise ServingError("runtime binary tree contains an unsafe entry")
    if not files:
        raise ServingError("runtime binary tree is empty")
    for relative, path in sorted(files):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def validate_binary_tree_against_archive(
    *,
    installed_directory: Path,
    archive: Path,
    anchor_member: str,
) -> None:
    """Verify every installed native sidecar under an archive-bound directory."""

    if not archive.is_file():
        raise ServingError("runtime binary tree archive is missing")
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = [item for item in bundle.infolist() if not item.is_dir()]
            anchors = [
                item
                for item in infos
                if PurePosixPath(item.filename).name == anchor_member
            ]
            if len(anchors) != 1:
                raise ServingError(
                    f"runtime archive must contain exactly one {anchor_member}"
                )
            prefix = PurePosixPath(anchors[0].filename).parent
            expected: dict[str, str] = {}
            for info in infos:
                member = PurePosixPath(info.filename)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or ((info.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ServingError(
                        "runtime archive binary tree contains an unsafe member"
                    )
                try:
                    relative = member.relative_to(prefix)
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                relative_text = relative.as_posix()
                if relative_text in expected:
                    raise ServingError(
                        "runtime archive binary tree contains duplicate members"
                    )
                expected[relative_text] = hashlib.sha256(
                    bundle.read(info)
                ).hexdigest()
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ServingError("runtime archive tree provenance is unreadable") from exc
    if not expected:
        raise ServingError("runtime archive binary tree is empty")
    installed: dict[str, str] = {}
    if not installed_directory.is_dir() or installed_directory.is_symlink():
        raise ServingError("installed runtime binary tree is missing or unsafe")
    for path in installed_directory.rglob("*"):
        if path.is_symlink():
            raise ServingError("installed runtime binary tree contains a link")
        if path.is_file():
            installed[path.relative_to(installed_directory).as_posix()] = (
                _sha256_file(path)
            )
        elif not path.is_dir():
            raise ServingError("installed runtime binary tree contains an unsafe entry")
    if expected.keys() != installed.keys() or any(
        not hmac.compare_digest(digest, installed[relative])
        for relative, digest in expected.items()
    ):
        raise ServingError(
            "installed runtime binary tree differs from policy archive provenance"
        )


def validate_runtime_freshness(
    *,
    root: Path,
    config: Path,
    admission_path: Path,
    llama_swap: Path,
    resources: ResourceSnapshot,
) -> tuple[dict[str, Any], dict[str, ContextPlan]]:
    """Revalidate generated state, model authority, binaries, and resources."""

    root = Path(os.path.abspath(os.fspath(root)))
    evidence, contexts = _load_admission(admission_path)
    expected_config = evidence.get("config_sha256")
    if (
        not isinstance(expected_config, str)
        or not SHA256.fullmatch(expected_config)
        or not config.is_file()
        or _sha256_file(config) != expected_config
    ):
        raise ServingError("generated config integrity is stale or invalid")
    raw_models = evidence.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise ServingError("runtime model evidence is missing")
    models = parse_manifest(
        _read_utf8(root / "serving" / "models.manifest", "model manifest")
    )
    profiles = parse_profiles(
        _read_utf8(root / "serving" / "profiles.conf", "profile declaration")
    )
    validate_profile_references(profiles, models)
    selected = tuple(raw_models)
    snapshots = validate_policy_bound_models(root, models, selected)
    for name, snapshot in snapshots.items():
        raw = raw_models.get(name)
        if not isinstance(raw, dict):
            raise ServingError(f"{name}: runtime model evidence is invalid")
        if (
            raw.get("authority_digest") != snapshot.authority_digest
            or raw.get("model_bytes") != snapshot.size_bytes
        ):
            raise ServingError(f"{name}: model authority/digest evidence is stale")
    tiers = evidence.get("tiers")
    if not isinstance(tiers, dict):
        raise ServingError("runtime tier mapping is missing")
    active_profile = resolve_active_profile(
        profiles,
        models,
        selected,
        {key: str(tiers.get(key, "")) for key in TIER_KEYS},
    )
    selection = evidence.get("selection")
    profile_state_path = (
        root / "state" / "generated" / "serving" / "profile.json"
    )
    try:
        profile_state = json.loads(
            _read_utf8(profile_state_path, "generated profile state")
        )
    except json.JSONDecodeError as exc:
        raise ServingError("generated profile state is invalid") from exc
    if (
        evidence.get("profile") != active_profile.name
        or not isinstance(selection, dict)
        or set(selection)
        != {"profile_explicit", "requested_profile", "selected_profile"}
        or not isinstance(selection.get("profile_explicit"), bool)
        or selection.get("selected_profile") != active_profile.name
        or not isinstance(profile_state, dict)
        or profile_state.get("schema_version") != 1
        or profile_state.get("profile") != active_profile.name
        or not isinstance(profile_state.get("models"), list)
        or set(profile_state["models"]) != set(selected)
    ):
        raise ServingError("generated active profile state is stale or invalid")
    active_path = root / "serving" / "models.profile"
    if active_path.is_file():
        current_active = parse_active_models(
            _read_utf8(active_path, "active model profile")
        )
        current_tiers = (
            parse_tiers(_read_utf8(root / "serving" / "tiers.env", "tier mapping"))
            if (root / "serving" / "tiers.env").is_file()
            else _profile_tiers(active_profile)
        )
        current_profile = resolve_active_profile(
            profiles,
            models,
            current_active,
            current_tiers,
        )
        if (
            selection.get("profile_explicit") is not True
            or selection.get("requested_profile") != current_profile.name
            or set(current_active) != set(selected)
        ):
            raise ServingError("runtime differs from the explicit active profile")
    else:
        currently_eligible = select_profile(profiles, resources)
        if (
            selection.get("profile_explicit") is not False
            or selection.get("requested_profile") != currently_eligible.name
        ):
            raise ServingError("automatic profile selection is stale for current resources")
    binaries = evidence.get("binaries")
    if not isinstance(binaries, dict):
        raise ServingError("runtime binary provenance is missing")
    raw_swap = binaries.get("llama_swap")
    if not isinstance(raw_swap, dict):
        raise ServingError("llama-swap binary provenance is missing")
    expected_swap_path = Path(str(raw_swap.get("path", "")))
    expected_swap_digest = str(raw_swap.get("sha256", ""))
    if (
        expected_swap_path != llama_swap
        or not SHA256.fullmatch(expected_swap_digest)
        or not llama_swap.is_file()
        or _sha256_file(llama_swap) != expected_swap_digest
    ):
        raise ServingError("llama-swap binary provenance does not match")
    raw_server = binaries.get("llama_server")
    if not isinstance(raw_server, dict):
        raise ServingError("llama-server binary provenance is missing")
    server_path = Path(str(raw_server.get("path", "")))
    server_digest = str(raw_server.get("sha256", ""))
    if (
        not SHA256.fullmatch(server_digest)
        or not server_path.is_file()
        or _sha256_file(server_path) != server_digest
    ):
        raise ServingError("llama-server binary provenance does not match")
    raw_server_tree = binaries.get("llama_server_tree")
    if server_path.suffix.lower() == ".exe" and not isinstance(
        raw_server_tree, dict
    ):
        raise ServingError("llama-server sidecar provenance is missing")
    if isinstance(raw_server_tree, dict):
        tree_path = Path(str(raw_server_tree.get("path", "")))
        tree_digest = str(raw_server_tree.get("sha256", ""))
        if (
            tree_path != server_path.parent
            or not SHA256.fullmatch(tree_digest)
            or _directory_tree_digest(tree_path) != tree_digest
        ):
            raise ServingError("llama-server binary tree provenance does not match")

    ram_available = max(
        0,
        resources.system_available_mib
        - resources.os_reserve_mib
        - resources.runtime_reserve_mib,
    )
    vram_available = max(
        0,
        resources.accelerator_available_mib - resources.accelerator_reserve_mib,
    )
    resident_ram = 0
    resident_vram = 0
    swappable_ram: list[int] = []
    swappable_vram: list[int] = []
    for name, raw in raw_models.items():
        if not isinstance(raw, dict):
            raise ServingError(f"{name}: runtime model evidence is invalid")
        placement = raw.get("placement")
        if not isinstance(placement, dict):
            raise ServingError(f"{name}: runtime placement evidence is missing")
        ram_required = placement.get("ram_required_mib")
        vram_required = placement.get("vram_required_mib")
        backend = placement.get("backend")
        if (
            not isinstance(ram_required, int)
            or isinstance(ram_required, bool)
            or not isinstance(vram_required, int)
            or isinstance(vram_required, bool)
            or ram_required > ram_available
            or vram_required < 0
        ):
            raise ServingError(f"{name}: current RAM cannot satisfy runtime placement")
        if backend != Backend.CPU.value:
            if resources.backend.value != backend or vram_required > vram_available:
                raise ServingError(
                    f"{name}: current accelerator resources cannot satisfy placement"
                )
        if raw.get("slot") in {"fast", "embed"}:
            resident_ram += ram_required
            resident_vram += vram_required
        else:
            swappable_ram.append(ram_required)
            swappable_vram.append(vram_required)
    aggregate_ram = resident_ram + max(swappable_ram, default=0)
    aggregate_vram = resident_vram + max(swappable_vram, default=0)
    if aggregate_ram > ram_available:
        raise ServingError(
            "current RAM cannot satisfy aggregate resident/swappable placement"
        )
    if aggregate_vram > vram_available:
        raise ServingError(
            "current VRAM cannot satisfy aggregate resident/swappable placement"
        )
    return evidence, contexts


class AdmissionLease:
    def __init__(self, semaphores: Sequence[threading.BoundedSemaphore]):
        self._semaphores = tuple(semaphores)
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            for semaphore in reversed(self._semaphores):
                semaphore.release()

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()


class AdmissionController:
    """Non-blocking per-model request admission used by the loopback gateway."""

    def __init__(
        self,
        contexts: Mapping[str, ContextPlan],
        *,
        exclusive_groups: Sequence[frozenset[str]] = (),
        aliases: Mapping[str, str] | None = None,
    ):
        if not contexts:
            raise ServingError("admission controller needs at least one model")
        self._contexts = dict(contexts)
        self._aliases = dict(aliases or {})
        for alias, target in self._aliases.items():
            if (
                not alias
                or alias in self._contexts
                or target not in self._contexts
                or alias in self._aliases.values()
            ):
                raise ServingError(f"model alias collision or invalid target: {alias}")
        self._semaphores = {
            name: threading.BoundedSemaphore(plan.parallel_slots)
            for name, plan in contexts.items()
        }
        self._group_semaphores: dict[
            str, list[threading.BoundedSemaphore]
        ] = {name: [] for name in contexts}
        for group in exclusive_groups:
            if len(group) < 2 or not group.issubset(contexts):
                raise ServingError("exclusive admission group is invalid")
            semaphore = threading.BoundedSemaphore(1)
            for name in sorted(group):
                self._group_semaphores[name].append(semaphore)

    def try_begin(
        self, model_name: str, payload: Mapping[str, Any]
    ) -> AdmissionLease:
        canonical_name = self._aliases.get(model_name, model_name)
        plan = self._contexts.get(canonical_name)
        if plan is None:
            raise ServingError(f"model is outside the admitted serving plan: {model_name}")
        dispatch_admitted(
            plan,
            payload,
            active_requests=0,
            invoke=lambda request: request,
        )
        semaphores = [
            self._semaphores[canonical_name],
            *self._group_semaphores[canonical_name],
        ]
        acquired: list[threading.BoundedSemaphore] = []
        for semaphore in semaphores:
            if semaphore.acquire(blocking=False):
                acquired.append(semaphore)
                continue
            for held in reversed(acquired):
                held.release()
            raise ServingError(
                f"{model_name}: unsafe contention; "
                "an admitted slot or exclusive model group is already active"
            )
        return AdmissionLease(acquired)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Mapping[str, str],
        _new_url: str,
    ) -> None:
        return None


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


def fetch_url_no_redirect(
    *,
    url: str,
    output: Path,
    allowed_hosts: set[str],
    allowed_schemes: set[str] | frozenset[str] = frozenset({"https"}),
    timeout: int = 300,
    maximum_bytes: int = 4 * 1024 * 1024 * 1024,
) -> None:
    """Fetch one allowlisted URL atomically while rejecting every redirect."""

    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() not in {item.lower() for item in allowed_schemes}
        or hostname not in {item.lower() for item in allowed_hosts}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not hostname
        or maximum_bytes <= 0
    ):
        raise ServingError("fetch URL is outside the allowlisted origin")
    output = Path(os.path.abspath(os.fspath(output)))
    if output.exists():
        raise ServingError("fetch output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".download", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(url, method="GET")
        try:
            response = _no_redirect_opener().open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ServingError(f"redirect/HTTP {exc.code} refused") from exc
            raise ServingError(f"fetch failed with HTTP {exc.code}") from exc
        with response, temporary.open("wb") as handle:
            if not 200 <= response.status < 300:
                raise ServingError(f"fetch failed with HTTP {response.status}")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise ServingError("fetch exceeded maximum byte count")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def create_admission_server(
    *,
    host: str,
    port: int,
    upstream: str,
    contexts: Mapping[str, ContextPlan],
    evidence: Mapping[str, Any],
    exclusive_groups: Sequence[frozenset[str]] = (),
    aliases: Mapping[str, str] | None = None,
) -> ThreadingHTTPServer:
    """Create the loopback gateway that rejects unsafe work before llama-swap."""

    require_loopback(host)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ServingError("gateway port is invalid")
    parsed_upstream = urllib.parse.urlsplit(upstream)
    if (
        parsed_upstream.scheme != "http"
        or not parsed_upstream.hostname
        or not _is_loopback_without_raise(parsed_upstream.hostname)
        or parsed_upstream.username
        or parsed_upstream.password
        or parsed_upstream.query
        or parsed_upstream.fragment
    ):
        raise ServingError("gateway upstream must be a credential-free loopback HTTP URL")
    controller = AdmissionController(
        contexts,
        exclusive_groups=exclusive_groups,
        aliases=aliases,
    )
    capability_payload = redact_sensitive(dict(evidence))
    maximum_body = max(
        1024 * 1024,
        max(plan.slot_context_tokens for plan in contexts.values()) * 32,
    )
    base = upstream.rstrip("/")

    class AdmissionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            # Request paths, headers, and payloads can contain credentials or prompts.
            return

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            admission_rejected: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if admission_rejected:
                self.send_header("X-Oracle-Admission", "rejected")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(
            self,
            status: int,
            payload: Mapping[str, Any],
            *,
            admission_rejected: bool = False,
        ) -> None:
            self._send(
                status,
                (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
                "application/json",
                admission_rejected=admission_rejected,
            )

        def _open_upstream(
            self, method: str, body: bytes | None
        ) -> Any:
            path = self.path
            if not path.startswith("/") or "\r" in path or "\n" in path:
                raise ServingError("unsafe upstream request path")
            target = base + path
            parsed_target = urllib.parse.urlsplit(target)
            if (
                parsed_target.scheme != parsed_upstream.scheme
                or parsed_target.hostname != parsed_upstream.hostname
                or parsed_target.port != parsed_upstream.port
            ):
                raise ServingError("upstream request escaped the configured loopback")
            headers = {}
            for header in (
                "Content-Type",
                "Accept",
                "Anthropic-Version",
                "Anthropic-Beta",
                "Authorization",
                "X-Api-Key",
            ):
                value = self.headers.get(header)
                if value:
                    headers[header] = value
            request = urllib.request.Request(
                target,
                data=body,
                headers=headers,
                method=method,
            )
            try:
                return _no_redirect_opener().open(request, timeout=1800)
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    exc.close()
                    raise ServingError(f"upstream redirect HTTP {exc.code} refused") from exc
                return exc

        def _forward(
            self, method: str, body: bytes | None, *, stream: bool = False
        ) -> None:
            response = self._open_upstream(method, body)
            with response:
                status = int(getattr(response, "status", getattr(response, "code", 0)))
                if not 100 <= status <= 599 or 300 <= status < 400:
                    raise ServingError(f"unsafe upstream HTTP status {status}")
                content_type = response.headers.get(
                    "Content-Type", "application/json"
                )
                streaming = stream or content_type.lower().startswith(
                    "text/event-stream"
                )
                if streaming and 200 <= status < 300:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-store")
                    request_id = response.headers.get("X-Request-Id")
                    if request_id and "\r" not in request_id and "\n" not in request_id:
                        self.send_header("X-Request-Id", request_id)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    total = 0
                    deadline = time.monotonic() + 1800
                    read_available = getattr(response, "read1", response.read)
                    while True:
                        if time.monotonic() > deadline:
                            self.close_connection = True
                            raise ServingError(
                                "upstream streaming response exceeded time limit"
                            )
                        chunk = read_available(4096)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > maximum_body:
                            self.close_connection = True
                            raise ServingError(
                                "upstream streaming response exceeded gateway limit"
                            )
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                response_body = response.read(maximum_body + 1)
                if len(response_body) > maximum_body:
                    raise ServingError("upstream response exceeded gateway limit")
                self._send(status, response_body, content_type)

        def do_GET(self) -> None:
            if self.path == "/oracle/capabilities":
                self._send_json(200, capability_payload)
                return
            try:
                self._forward("GET", None)
            except (OSError, ServingError, urllib.error.URLError) as exc:
                self._send_json(
                    503,
                    {"error": "loopback upstream unavailable", "reason": str(exc)},
                )

        def do_POST(self) -> None:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "")
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if length <= 0 or length > maximum_body:
                self._send_json(
                    413,
                    {"error": "request body exceeds admitted gateway limit"},
                    admission_rejected=True,
                )
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "request body must be UTF-8 JSON"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "request JSON must be an object"})
                return
            if (
                self.path in {"/v1/chat/completions", "/v1/messages"}
                and "max_completion_tokens" in payload
                and "max_tokens" not in payload
            ):
                payload["max_tokens"] = payload.pop(
                    "max_completion_tokens"
                )
            if (
                self.path in {"/v1/chat/completions", "/v1/messages"}
                and "max_tokens" not in payload
                and "max_completion_tokens" not in payload
            ):
                payload["max_tokens"] = DEFAULT_REQUEST_OUTPUT_TOKENS
            if self.path in {"/v1/chat/completions", "/v1/messages"}:
                try:
                    body = json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    self._send_json(
                        400,
                        {"error": "request JSON contains unsupported values"},
                    )
                    return
            model = payload.get("model")
            if not isinstance(model, str) or not model:
                self._send_json(400, {"error": "request model is required"})
                return
            try:
                lease = controller.try_begin(model, payload)
            except ServingError as exc:
                reason = str(exc)
                status = 429 if "contention" in reason else 413 if "context" in reason else 400
                self._send_json(
                    status,
                    {"error": "Oracle admission rejected request", "reason": reason},
                    admission_rejected=True,
                )
                return
            with lease:
                try:
                    self._forward(
                        "POST",
                        body,
                        stream=payload.get("stream") is True,
                    )
                except (OSError, ServingError, urllib.error.URLError) as exc:
                    if not self.close_connection:
                        self._send_json(
                            503,
                            {
                                "error": "loopback upstream unavailable",
                                "reason": str(exc),
                            },
                        )

    server = ThreadingHTTPServer((host, port), AdmissionHandler)
    server.daemon_threads = True
    return server


def parse_runtime_config(text: str) -> dict[str, Any]:
    """Parse the deliberately small generated YAML subset for verification."""

    parsed: dict[str, Any] = {"models": {}}
    in_models = False
    current: str | None = None
    expect_command = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "models:":
            in_models = True
            continue
        if stripped == "groups:":
            in_models = False
            current = None
            continue
        if not in_models:
            continue
        model_match = re.fullmatch(r'  "([^"]+)":', raw)
        if model_match:
            current = model_match.group(1)
            parsed["models"][current] = {}
            expect_command = False
            continue
        if current is None:
            continue
        if raw == "    cmd: >-":
            expect_command = True
            continue
        if expect_command and raw.startswith("      "):
            parsed["models"][current]["cmd"] = raw[6:]
            expect_command = False
            continue
        context_match = re.fullmatch(
            r"\s*# oracle-advertised-context:\s*([0-9]+)", raw
        )
        if context_match:
            parsed["models"][current]["advertised_context"] = int(
                context_match.group(1)
            )
    for name, model in parsed["models"].items():
        if "cmd" not in model or "advertised_context" not in model:
            raise ServingError(f"generated config model {name} is incomplete")
    return parsed


def command_line_to_argv_windows(command: str) -> list[str]:
    """Parse a CommandLineToArgvW-compatible command line on any host."""

    argv: list[str] = []
    index = 0
    length = len(command)
    while index < length:
        while index < length and command[index] in " \t":
            index += 1
        if index >= length:
            break
        argument: list[str] = []
        quoted = False
        while index < length:
            if command[index] in " \t" and not quoted:
                break
            backslashes = 0
            while index < length and command[index] == "\\":
                backslashes += 1
                index += 1
            if index < length and command[index] == '"':
                argument.extend("\\" for _ in range(backslashes // 2))
                if backslashes % 2:
                    argument.append('"')
                else:
                    quoted = not quoted
                index += 1
            else:
                argument.extend("\\" for _ in range(backslashes))
                if index < length and not (
                    command[index] in " \t" and not quoted
                ):
                    argument.append(command[index])
                    index += 1
        argv.append("".join(argument))
        while index < length and command[index] in " \t":
            index += 1
    return argv


def process_matches_pid_record(
    record: PidRecord,
    *,
    executable: str,
    started_at: float,
    command_digest: str,
) -> bool:
    executable_matches = os.path.normcase(os.path.abspath(executable)) == os.path.normcase(
        os.path.abspath(record.executable)
    )
    return (
        record.pid > 0
        and executable_matches
        and abs(record.started_at - started_at) < 0.001
        and record.command_digest == command_digest
    )


def stop_recorded_process(
    record: PidRecord,
    *,
    inspect: Callable[[int], tuple[str, float, str] | None],
    terminate: Callable[[int], Any],
) -> None:
    observed = inspect(record.pid)
    if observed is None or not process_matches_pid_record(
        record,
        executable=observed[0],
        started_at=observed[1],
        command_digest=observed[2],
    ):
        raise ServingError(
            f"refusing to stop PID {record.pid}: process identity does not match"
        )
    terminate(record.pid)


_SERVICE_LOCK_HANDLES: dict[tuple[str, str], Any] = {}
_SERVICE_LOCK_GUARD = threading.Lock()


def _try_lock_service_file(handle: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        return False
    return True


def _unlock_service_file(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def acquire_service_lock(
    state: Path,
    record: PidRecord,
    *,
    inspect: Callable[[int], tuple[str, float, str] | None],
) -> str:
    """Acquire and retain an OS-backed singleton lock for this process."""

    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "service.lock"
    owner = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "owner": owner,
        "pid": record.pid,
        "executable": record.executable,
        "started_at": record.started_at,
        "command_digest": record.command_digest,
    }
    handle = lock_path.open("a+b")
    if not _try_lock_service_file(handle):
        handle.close()
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
            existing = PidRecord(
                int(current["pid"]),
                str(current["executable"]),
                float(current["started_at"]),
                str(current["command_digest"]),
            )
            observed = inspect(existing.pid)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            observed = None
            existing = None
        if (
            existing is not None
            and observed is not None
            and process_matches_pid_record(
                existing,
                executable=observed[0],
                started_at=observed[1],
                command_digest=observed[2],
            )
        ):
            raise ServingError("serving service already has an active owner")
        raise ServingError(
            "serving service lock is held by another process; refusing ownership race"
        )
    try:
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        handle.seek(0)
        handle.truncate()
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        key = (os.path.normcase(os.path.abspath(lock_path)), owner)
        with _SERVICE_LOCK_GUARD:
            _SERVICE_LOCK_HANDLES[key] = handle
        return owner
    except BaseException:
        _unlock_service_file(handle)
        raise


def release_service_lock(state: Path, owner: str) -> bool:
    """Release only the singleton lock created by the supplied owner token."""

    lock_path = state / "service.lock"
    key = (os.path.normcase(os.path.abspath(lock_path)), owner)
    with _SERVICE_LOCK_GUARD:
        handle = _SERVICE_LOCK_HANDLES.pop(key, None)
    if handle is None:
        return False
    try:
        handle.seek(0)
        payload = json.loads(handle.read().decode("utf-8"))
        owned = isinstance(payload, dict) and payload.get("owner") == owner
        if owned:
            released = {
                "schema_version": 1,
                "released_owner": owner,
            }
            handle.seek(0)
            handle.truncate()
            handle.write(
                (json.dumps(released, sort_keys=True) + "\n").encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        return owned
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    finally:
        _unlock_service_file(handle)


def _probe(
    name: str,
    status: str,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> ProbeResult:
    return ProbeResult(name, status, reason, redact_sensitive(evidence or {}))


def _response_ok(response: HttpResponse) -> bool:
    return 200 <= response.status < 300


def _listener_host(listener: str) -> str:
    value = listener.strip()
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


def run_offline_probes(
    *,
    transport: Callable[[str, str, dict[str, object] | None], HttpResponse],
    contexts: Mapping[str, ContextPlan],
    chat_model: str,
    embedding_model: str,
    listeners: Sequence[str],
    engine_runner: Callable[[str], tuple[int, str]] | None,
    planned_placements: Mapping[str, Mapping[str, Any] | PlacementPlan] | None = None,
    listener_inspector: Callable[[int], Sequence[str]] | None = None,
    expected_listener_ports: Mapping[str, int] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[ProbeResult]:
    """Run read-only production-shaped compatibility probes."""

    def note(message: str) -> None:
        if progress is not None:
            progress(message)

    note("checking the local service and model list")
    results: list[ProbeResult] = []
    cold_running = transport("GET", "/running", None)
    cold_entries: list[Any] = []
    if _response_ok(cold_running) and isinstance(cold_running.body, dict):
        raw_cold = cold_running.body.get("running", [])
        if isinstance(raw_cold, list):
            cold_entries = raw_cold
    health = transport("GET", "/health", None)
    results.append(
        _probe(
            "health",
            PASS if _response_ok(health) else FAIL,
            "loopback service is healthy" if _response_ok(health) else "health failed",
            {"http_status": health.status, "elapsed_ms": health.elapsed_ms},
        )
    )
    models_response = transport("GET", "/v1/models", None)
    model_ids: set[str] = set()
    if _response_ok(models_response) and isinstance(models_response.body, dict):
        data = models_response.body.get("data", [])
        if isinstance(data, list):
            model_ids = {
                str(item.get("id"))
                for item in data
                if isinstance(item, dict) and item.get("id")
            }
    expected_models = {
        chat_model,
        embedding_model,
        *(plan.model_name for plan in contexts.values()),
    }
    identity_ok = expected_models.issubset(model_ids)
    results.append(
        _probe(
            "model_identity",
            PASS if identity_ok else FAIL,
            "advertised model identities match the admitted plan"
            if identity_ok
            else "admitted model identities are absent from /v1/models",
            {
                "http_status": models_response.status,
                "models": sorted(model_ids),
                "missing": sorted(expected_models - model_ids),
            },
        )
    )
    note("probing the chat model (first model load can take a few minutes)")
    plan = contexts.get(chat_model)
    if plan is None:
        results.append(_probe("openai_chat", FAIL, "chat model has no admission plan"))
        return results
    additional_running_entries: list[Any] = []
    covered_models: set[str] = set()
    additional_chat_models = sorted(
        name
        for name in contexts
        if name not in {chat_model, embedding_model}
    )
    for model_name in additional_chat_models:
        model_plan = contexts[model_name]
        production_scale = (
            model_plan.advertised_context_tokens
            >= PRODUCTION_ADVERTISED_CONTEXT
        )
        model_payload: dict[str, object] = {
            "model": model_name,
            "max_tokens": min(512, model_plan.output_reserve_tokens),
            "messages": [
                {
                    "role": "user",
                    "content": (
                        " x" * 25000
                        if production_scale
                        else "Exercise this admitted local model."
                    ),
                }
            ],
        }
        model_estimate = estimate_request_tokens(model_payload)
        model_required = (
            model_estimate.total_tokens
            + model_plan.prompt_tool_overhead_tokens
            + model_plan.output_reserve_tokens
        )
        if model_required > model_plan.slot_context_tokens:
            model_chat = HttpResponse(
                0,
                {"error": "production request exceeds admitted context"},
                {},
                0,
            )
            model_chat_ok = False
            observed_prompt_tokens = 0
        else:
            model_chat = transport(
                "POST", "/v1/chat/completions", model_payload
            )
            observed_prompt_tokens = 0
            if isinstance(model_chat.body, dict):
                usage = model_chat.body.get("usage")
                if isinstance(usage, dict):
                    raw_prompt_tokens = usage.get("prompt_tokens")
                    if isinstance(raw_prompt_tokens, int) and not isinstance(
                        raw_prompt_tokens, bool
                    ):
                        observed_prompt_tokens = raw_prompt_tokens
            model_chat_ok = (
                _response_ok(model_chat)
                and isinstance(model_chat.body, dict)
                and isinstance(model_chat.body.get("choices"), list)
                and bool(model_chat.body["choices"])
                and (
                    not production_scale
                    or observed_prompt_tokens >= 25000
                )
            )
        results.append(
            _probe(
                f"openai_chat:{model_name}",
                PASS if model_chat_ok else FAIL,
                "model accepted a production-shaped chat request"
                if model_chat_ok
                else "model did not prove its admitted chat capacity",
                {
                    "http_status": model_chat.status,
                    "estimated_prompt_tokens": model_estimate.prompt_tokens,
                    "observed_prompt_tokens": observed_prompt_tokens,
                    "production_scale": production_scale,
                },
            )
        )
        boundary_output = min(128, model_plan.output_reserve_tokens)
        boundary_available = max(
            1,
            model_plan.slot_context_tokens
            - model_plan.prompt_tool_overhead_tokens
            - model_plan.output_reserve_tokens,
        )
        boundary_base: dict[str, object] = {
            "model": model_name,
            "max_tokens": boundary_output,
            "messages": [{"role": "user", "content": "boundary "}],
        }
        boundary_base_tokens = estimate_request_tokens(
            boundary_base
        ).total_tokens
        boundary_payload = dict(boundary_base)
        boundary_payload["messages"] = [
            {
                "role": "user",
                "content": "boundary "
                + ("a" * max(1, boundary_available - boundary_base_tokens)),
            }
        ]
        boundary = transport(
            "POST", "/v1/chat/completions", boundary_payload
        )
        boundary_ok = _response_ok(boundary)
        results.append(
            _probe(
                f"context_boundary:{model_name}",
                PASS if boundary_ok else FAIL,
                "model accepted its admitted request boundary"
                if boundary_ok
                else "model rejected its admitted request boundary",
                {"http_status": boundary.status},
            )
        )
        oversize = transport(
            "POST",
            "/v1/chat/completions",
            {
                "model": model_name,
                "max_tokens": model_plan.output_reserve_tokens,
                "messages": [
                    {
                        "role": "user",
                        "content": "oversize-boundary "
                        + (
                            "a"
                            * (
                                model_plan.slot_context_tokens
                                + model_plan.prompt_tool_overhead_tokens
                            )
                        ),
                    }
                ],
            },
        )
        oversize_ok = (
            oversize.status in {400, 413, 429}
            and str(
                oversize.headers.get("X-Oracle-Admission", "")
            ).lower()
            == "rejected"
        )
        results.append(
            _probe(
                f"context_oversize:{model_name}",
                PASS if oversize_ok else FAIL,
                "model rejected oversize work before upstream"
                if oversize_ok
                else "model did not prove pre-upstream oversize rejection",
                {"http_status": oversize.status},
            )
        )
        model_running = transport("GET", "/running", None)
        if _response_ok(model_running) and isinstance(
            model_running.body, dict
        ):
            raw_running = model_running.body.get("running", [])
            if isinstance(raw_running, list):
                additional_running_entries.extend(raw_running)
        if model_chat_ok and boundary_ok and oversize_ok:
            covered_models.add(model_name)
    production_output = min(512, plan.output_reserve_tokens)
    production_scale = (
        plan.advertised_context_tokens >= PRODUCTION_ADVERTISED_CONTEXT
    )
    production_payload: dict[str, object] = {
        "model": chat_model,
        "max_tokens": production_output,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an offline code agent. Respect tool and JSON contracts."
                ),
            },
            {
                "role": "user",
                "content": (
                    " x" * 25000
                    if production_scale
                    else "Exercise this admitted local model."
                ),
            },
        ],
    }
    production_estimate = estimate_request_tokens(production_payload)
    production_required = (
        production_estimate.total_tokens
        + plan.prompt_tool_overhead_tokens
        + plan.output_reserve_tokens
    )
    chat_ok = False
    if production_required > plan.slot_context_tokens:
        results.append(
            _probe(
                "openai_chat",
                FAIL,
                "admitted context cannot fit its production-shaped request",
                {
                    "admitted_prompt_budget": plan.advertised_context_tokens,
                    "slot_context": plan.slot_context_tokens,
                    "required_context": production_required,
                },
            )
        )
    else:
        chat = transport("POST", "/v1/chat/completions", production_payload)
        observed_prompt_tokens = 0
        if isinstance(chat.body, dict):
            usage = chat.body.get("usage")
            if isinstance(usage, dict):
                raw_prompt_tokens = usage.get("prompt_tokens")
                if isinstance(raw_prompt_tokens, int) and not isinstance(
                    raw_prompt_tokens, bool
                ):
                    observed_prompt_tokens = raw_prompt_tokens
        chat_ok = (
            _response_ok(chat)
            and isinstance(chat.body, dict)
            and isinstance(chat.body.get("choices"), list)
            and bool(chat.body["choices"])
            and (
                not production_scale
                or observed_prompt_tokens >= 25000
            )
        )
        results.append(
            _probe(
                "openai_chat",
                PASS if chat_ok else FAIL,
                "production-shaped OpenAI chat accepted"
                if chat_ok
                else (
                    "production-shaped chat failed at admitted scale "
                    f"(HTTP {chat.status})"
                ),
                {
                    "http_status": chat.status,
                    "estimated_prompt_tokens": estimate_request_tokens(
                        production_payload
                    ).prompt_tokens,
                    "observed_prompt_tokens": observed_prompt_tokens,
                    "production_scale": production_scale,
                    "elapsed_ms": chat.elapsed_ms,
                },
            )
        )
    tool_payload: dict[str, object] = {
        "model": chat_model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Call oracle_probe exactly once."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "oracle_probe",
                    "description": "Return local verification evidence.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "required",
    }
    tools = transport("POST", "/v1/chat/completions", tool_payload)
    tool_ok = False
    if _response_ok(tools) and isinstance(tools.body, dict):
        choices = tools.body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            tool_ok = isinstance(message, dict) and bool(message.get("tool_calls"))
    results.append(
        _probe(
            "openai_tools",
            PASS if tool_ok else FAIL,
            "OpenAI tool calling is compatible"
            if tool_ok
            else f"tool call contract failed (HTTP {tools.status})",
            {"http_status": tools.status},
        )
    )
    json_payload: dict[str, object] = {
        "model": chat_model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": 'Return JSON: {"ok": true}'}],
        "response_format": {"type": "json_object"},
    }
    json_response = transport("POST", "/v1/chat/completions", json_payload)
    json_ok = False
    if _response_ok(json_response) and isinstance(json_response.body, dict):
        try:
            content = json_response.body["choices"][0]["message"]["content"]
            parsed_content = json.loads(content)
            json_ok = isinstance(parsed_content, dict)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            json_ok = False
    results.append(
        _probe(
            "openai_json",
            PASS if json_ok else FAIL,
            "OpenAI JSON mode returned parseable JSON"
            if json_ok
            else f"JSON contract failed (HTTP {json_response.status})",
            {"http_status": json_response.status},
        )
    )
    anthropic_payload: dict[str, object] = {
        "model": chat_model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Reply with ORACLE-OK."}],
    }
    anthropic = transport("POST", "/v1/messages", anthropic_payload)
    anthropic_ok = (
        _response_ok(anthropic)
        and isinstance(anthropic.body, dict)
        and isinstance(anthropic.body.get("content"), list)
        and bool(anthropic.body["content"])
    )
    results.append(
        _probe(
            "anthropic_messages",
            PASS if anthropic_ok else FAIL,
            "Anthropic Messages compatibility passed"
            if anthropic_ok
            else f"Anthropic contract failed (HTTP {anthropic.status})",
            {"http_status": anthropic.status},
        )
    )
    note("probing the embedding model")
    embedding = transport(
        "POST",
        "/v1/embeddings",
        {"model": embedding_model, "input": "offline embedding compatibility probe"},
    )
    embedding_length = 0
    if _response_ok(embedding) and isinstance(embedding.body, dict):
        try:
            vector = embedding.body["data"][0]["embedding"]
            embedding_length = len(vector) if isinstance(vector, list) else 0
        except (KeyError, IndexError, TypeError):
            embedding_length = 0
    results.append(
        _probe(
            "embeddings",
            PASS if embedding_length >= 8 else FAIL,
            "embedding vector returned"
            if embedding_length >= 8
            else f"embedding contract failed (HTTP {embedding.status})",
            {"http_status": embedding.status, "dimensions": embedding_length},
        )
    )
    embedding_running_entries: list[Any] = []
    embedding_plan = contexts.get(embedding_model)
    if embedding_plan is not None:
        embedding_available = max(
            1,
            embedding_plan.slot_context_tokens
            - embedding_plan.prompt_tool_overhead_tokens
            - embedding_plan.output_reserve_tokens,
        )
        embedding_base: dict[str, object] = {
            "model": embedding_model,
            "input": "embedding-boundary ",
        }
        embedding_base_tokens = estimate_request_tokens(
            embedding_base
        ).total_tokens
        embedding_boundary_payload = {
            "model": embedding_model,
            "input": "embedding-boundary "
            + (
                "a"
                * max(
                    1,
                    embedding_available - embedding_base_tokens,
                )
            ),
        }
        embedding_boundary = transport(
            "POST",
            "/v1/embeddings",
            embedding_boundary_payload,
        )
        embedding_boundary_ok = _response_ok(embedding_boundary)
        results.append(
            _probe(
                "embedding_context_boundary",
                PASS if embedding_boundary_ok else FAIL,
                "embedding model accepted its admitted request boundary"
                if embedding_boundary_ok
                else "embedding model rejected its admitted request boundary",
                {"http_status": embedding_boundary.status},
            )
        )
        embedding_oversize = transport(
            "POST",
            "/v1/embeddings",
            {
                "model": embedding_model,
                "input": "oversize-embedding "
                + (
                    "a"
                    * (
                        embedding_plan.slot_context_tokens
                        + embedding_plan.prompt_tool_overhead_tokens
                    )
                ),
            },
        )
        embedding_oversize_ok = (
            embedding_oversize.status in {400, 413, 429}
            and str(
                embedding_oversize.headers.get(
                    "X-Oracle-Admission", ""
                )
            ).lower()
            == "rejected"
        )
        results.append(
            _probe(
                "embedding_context_oversize",
                PASS if embedding_oversize_ok else FAIL,
                "embedding oversize work was rejected before upstream"
                if embedding_oversize_ok
                else "embedding oversize rejection was not proven",
                {"http_status": embedding_oversize.status},
            )
        )
        embedding_running = transport("GET", "/running", None)
        if _response_ok(embedding_running) and isinstance(
            embedding_running.body, dict
        ):
            raw_embedding_running = embedding_running.body.get(
                "running", []
            )
            if isinstance(raw_embedding_running, list):
                embedding_running_entries.extend(raw_embedding_running)
    boundary_output = min(128, plan.output_reserve_tokens)
    boundary_available = max(
        1,
        plan.slot_context_tokens
        - plan.prompt_tool_overhead_tokens
        - plan.output_reserve_tokens
    )
    boundary_base: dict[str, object] = {
        "model": chat_model,
        "max_tokens": boundary_output,
        "messages": [
            {"role": "user", "content": "boundary "}
        ],
    }
    boundary_base_tokens = estimate_request_tokens(boundary_base).total_tokens
    boundary_filler = max(1, boundary_available - boundary_base_tokens)
    boundary_payload = dict(boundary_base)
    boundary_payload["messages"] = [
        {"role": "user", "content": "boundary " + ("a" * boundary_filler)}
    ]
    boundary = transport("POST", "/v1/chat/completions", boundary_payload)
    boundary_ok = _response_ok(boundary)
    results.append(
        _probe(
            "context_boundary",
            PASS if boundary_ok else FAIL,
            "admitted request boundary accepted"
            if boundary_ok
            else f"admitted context boundary rejected (HTTP {boundary.status})",
            {
                "http_status": boundary.status,
                "advertised_context": plan.advertised_context_tokens,
                "slot_context": plan.slot_context_tokens,
            },
        )
    )
    oversize_payload: dict[str, object] = {
        "model": chat_model,
        "max_tokens": plan.output_reserve_tokens,
        "messages": [
            {
                "role": "user",
                "content": "oversize-boundary "
                + ("a" * (plan.slot_context_tokens + plan.prompt_tool_overhead_tokens)),
            }
        ],
    }
    oversize = transport("POST", "/v1/chat/completions", oversize_payload)
    rejected_before_upstream = (
        oversize.status in {400, 413, 429}
        and str(oversize.headers.get("X-Oracle-Admission", "")).lower()
        == "rejected"
    )
    results.append(
        _probe(
            "context_oversize",
            PASS if rejected_before_upstream else FAIL,
            "oversize request rejected by Oracle admission before upstream"
            if rejected_before_upstream
            else "oversize request was not proven rejected before upstream",
            {
                "http_status": oversize.status,
                "admission_header": oversize.headers.get(
                    "X-Oracle-Admission", ""
                ),
            },
        )
    )
    if chat_ok and boundary_ok and rejected_before_upstream:
        covered_models.add(chat_model)
    expected_chat_models = set(contexts) - {embedding_model}
    uncovered_models = sorted(expected_chat_models - covered_models)
    results.append(
        _probe(
            "model_context_coverage",
            PASS if not uncovered_models else FAIL,
            "every admitted chat model passed generation and context boundaries"
            if not uncovered_models
            else "one or more admitted chat models were not exercised successfully",
            {
                "covered": sorted(covered_models),
                "missing": uncovered_models,
            },
        )
    )
    running = transport("GET", "/running", None)
    running_entries: list[Any] = []
    if _response_ok(running) and isinstance(running.body, dict):
        raw_running = running.body.get("running", [])
        if isinstance(raw_running, list):
            running_entries = raw_running
    all_running_entries = [
        *additional_running_entries,
        *embedding_running_entries,
        *running_entries,
    ]
    cold_ready = any(
        isinstance(item, dict)
        and item.get("model") == chat_model
        and str(item.get("state", "")).lower() in {"ready", "running", "loaded"}
        for item in cold_entries
    )
    ready = any(
        isinstance(item, dict)
        and item.get("model") == chat_model
        and str(item.get("state", "")).lower() in {"ready", "running", "loaded"}
        for item in running_entries
    )
    results.append(
        _probe(
            "cold_warm_state",
            PASS if ready else FAIL,
            "runtime transitioned from cold to warm/ready"
            if ready and not cold_ready
            else (
                "runtime was already warm/ready when verification began"
                if ready and cold_ready
                else "runtime did not reach a warm/ready state"
            ),
            {
                "cold_http_status": cold_running.status,
                "warm_http_status": running.status,
                "cold": cold_entries,
                "warm": running_entries,
            },
        )
    )
    loaded_entry = next(
        (
            entry
            for entry in running_entries
            if isinstance(entry, Mapping)
            and entry.get("model") == chat_model
            and str(entry.get("state", "")).lower()
            in {"ready", "running", "loaded"}
        ),
        None,
    )
    loaded_backend = None
    offloaded_layers = None
    if isinstance(loaded_entry, Mapping):
        loaded_backend = next(
            (
                str(loaded_entry[key]).lower()
                for key in ("loaded_backend", "backend", "device")
                if loaded_entry.get(key) not in (None, "")
            ),
            None,
        )
        offloaded_layers = next(
            (
                int(loaded_entry[key])
                for key in ("offloaded_layers", "gpu_layers", "n_gpu_layers")
                if isinstance(loaded_entry.get(key), int)
                and not isinstance(loaded_entry.get(key), bool)
            ),
            None,
        )
    normalized_loaded = None
    if loaded_backend is not None:
        normalized_loaded = next(
            (
                backend.value
                for backend in Backend
                if backend.value in loaded_backend.lower()
            ),
            loaded_backend.lower(),
        )
    planned = (planned_placements or {}).get(chat_model)
    if isinstance(planned, PlacementPlan):
        expected_backend = planned.backend.value
        expected_offload = planned.offloaded_layers
    elif isinstance(planned, Mapping):
        expected_backend = str(planned.get("backend", "")).lower()
        raw_expected_offload = planned.get("offloaded_layers")
        expected_offload = (
            int(raw_expected_offload)
            if isinstance(raw_expected_offload, int)
            and not isinstance(raw_expected_offload, bool)
            else None
        )
    else:
        expected_backend = ""
        expected_offload = None
    observed = normalized_loaded is not None and offloaded_layers is not None
    placement_matches = observed
    if expected_backend:
        placement_matches = (
            normalized_loaded == expected_backend
            and offload_evidence_matches(
                expected_backend, expected_offload, offloaded_layers
            )
        )
    results.append(
        _probe(
            "loaded_backend",
            (
                PASS
                if placement_matches
                else FAIL
                if expected_backend and observed
                else PROVISIONAL
            ),
            "runtime placement matches selected backend and planned offload"
            if placement_matches
            else (
                "runtime loaded backend/offload differs from the serving plan"
                if expected_backend and observed
                else "runtime endpoint did not report both loaded backend and offloaded layers"
            ),
            {
                "loaded_backend": normalized_loaded,
                "offloaded_layers": offloaded_layers,
                "expected_backend": expected_backend or None,
                "expected_offloaded_layers": expected_offload,
                "source": "/running",
            },
        )
    )
    placement_probe_models = [
        *additional_chat_models,
        *([embedding_model] if embedding_model in contexts else []),
    ]
    for model_name in placement_probe_models:
        model_entry = next(
            (
                entry
                for entry in all_running_entries
                if isinstance(entry, Mapping)
                and entry.get("model") == model_name
                and str(entry.get("state", "")).lower()
                in {"ready", "running", "loaded"}
            ),
            None,
        )
        model_backend = None
        model_offload = None
        if isinstance(model_entry, Mapping):
            model_backend = next(
                (
                    str(model_entry[key]).lower()
                    for key in ("loaded_backend", "backend", "device")
                    if model_entry.get(key) not in (None, "")
                ),
                None,
            )
            model_offload = next(
                (
                    int(model_entry[key])
                    for key in (
                        "offloaded_layers",
                        "gpu_layers",
                        "n_gpu_layers",
                    )
                    if isinstance(model_entry.get(key), int)
                    and not isinstance(model_entry.get(key), bool)
                ),
                None,
            )
        normalized_backend = None
        if model_backend is not None:
            normalized_backend = next(
                (
                    item.value
                    for item in Backend
                    if item.value in model_backend
                ),
                model_backend,
            )
        model_planned = (planned_placements or {}).get(model_name)
        if isinstance(model_planned, PlacementPlan):
            model_expected_backend = model_planned.backend.value
            model_expected_offload = model_planned.offloaded_layers
        elif isinstance(model_planned, Mapping):
            model_expected_backend = str(
                model_planned.get("backend", "")
            ).lower()
            raw_model_expected_offload = model_planned.get(
                "offloaded_layers"
            )
            model_expected_offload = (
                int(raw_model_expected_offload)
                if isinstance(raw_model_expected_offload, int)
                and not isinstance(raw_model_expected_offload, bool)
                else None
            )
        else:
            model_expected_backend = ""
            model_expected_offload = None
        model_placement_matches = (
            normalized_backend is not None
            and model_offload is not None
        )
        model_placement_observed = model_placement_matches
        if model_expected_backend:
            model_placement_matches = (
                normalized_backend == model_expected_backend
                and offload_evidence_matches(
                    model_expected_backend,
                    model_expected_offload,
                    model_offload,
                )
            )
        results.append(
            _probe(
                f"loaded_backend:{model_name}",
                (
                    PASS
                    if model_placement_matches
                    else FAIL
                    if model_expected_backend and model_placement_observed
                    else PROVISIONAL
                ),
                "runtime placement matches the plan"
                if model_placement_matches
                else "runtime placement evidence differs or is absent",
                {
                    "loaded_backend": normalized_backend,
                    "offloaded_layers": model_offload,
                    "expected_backend": model_expected_backend or None,
                    "expected_offloaded_layers": model_expected_offload,
                },
            )
        )
    observed_listeners = set(listeners)
    ports = dict(expected_listener_ports or {})
    inspected_ports: dict[int, tuple[str, ...]] = {}
    missing_model_ports: list[str] = []
    if listener_inspector is not None:
        if not ports:
            ports = {"public": 9099, "internal": 9098}
        for entry in all_running_entries:
            if isinstance(entry, Mapping):
                raw_port = entry.get("port")
                if isinstance(raw_port, int) and not isinstance(raw_port, bool):
                    ports[f"model:{entry.get('model', raw_port)}"] = raw_port
                elif str(entry.get("state", "")).lower() in {
                    "ready",
                    "running",
                    "loaded",
                }:
                    missing_model_ports.append(
                        f"model:{entry.get('model', 'unknown')}:port-evidence"
                    )
        for port in sorted(set(ports.values())):
            inspected_ports[port] = tuple(listener_inspector(port))
            observed_listeners.update(inspected_ports[port])
    unsafe_listeners = [
        listener
        for listener in sorted(observed_listeners)
        if _listener_host(listener)
        and not _is_loopback_without_raise(_listener_host(listener))
    ]
    missing_ports = list(missing_model_ports)
    if listener_inspector is not None:
        for label, port in ports.items():
            if not inspected_ports.get(port):
                missing_ports.append(label)
    listeners_ok = bool(observed_listeners) and not unsafe_listeners and not missing_ports
    results.append(
        _probe(
            "loopback_binding",
            PASS if listeners_ok else FAIL,
            "all public, internal, and model listeners are loopback"
            if listeners_ok
            else "non-loopback or missing runtime listener evidence",
            {
                "listeners": sorted(observed_listeners),
                "unsafe": unsafe_listeners,
                "missing": missing_ports,
                "expected_ports": ports,
            },
        )
    )
    if engine_runner is not None:
        note("probing headless engine sessions (real model inference)")
    for engine in ("claude", "opencode"):
        if engine_runner is None:
            results.append(
                _probe(
                    f"headless_{engine}",
                    SKIP,
                    "headless engine runner is not provisioned",
                )
            )
            continue
        exit_code, output = engine_runner(engine)
        passed = exit_code == 0 and "ENGINE-OK" in output
        results.append(
            _probe(
                f"headless_{engine}",
                PASS if passed else FAIL,
                f"{engine} headless flow passed"
                if passed
                else f"{engine} headless flow failed",
                {"exit_code": exit_code, "output": output[-512:]},
            )
        )
    return results


def _is_loopback_without_raise(value: str) -> bool:
    try:
        require_loopback(value)
        return True
    except ServingError:
        return False


def aggregate_probe_status(results: Sequence[ProbeResult]) -> str:
    statuses = {result.status for result in results}
    if FAIL in statuses:
        return FAIL
    if not results or SKIP in statuses or PROVISIONAL in statuses:
        return PROVISIONAL
    return PASS if statuses == {PASS} else PROVISIONAL


def redact_sensitive(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_sensitive(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        redacted = SENSITIVE_URL_CREDENTIALS.sub(
            r"\1[REDACTED]@", value
        )
        return SENSITIVE_VALUE.sub("[REDACTED]", redacted)
    return value


def inspect_firewall_coverage(
    *,
    expected_classes: set[str],
    resolved: Mapping[str, Sequence[str]],
    read_only: bool,
) -> InspectionResult:
    if not read_only:
        raise ServingError("firewall coverage validation is read-only inspection only")
    missing = sorted(
        name for name in expected_classes if not tuple(resolved.get(name, ()))
    )
    return InspectionResult(
        PASS if not missing else FAIL,
        "all expected firewall target classes resolve"
        if not missing
        else "missing firewall target classes: " + ", ".join(missing),
        {
            "read_only": True,
            "expected_classes": sorted(expected_classes),
            "resolved": {
                name: list(paths) for name, paths in sorted(resolved.items())
            },
        },
    )


def safe_output_name(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}", value)
    ):
        raise ServingError("Envoy output must be one portable basename")
    return value


def _instruction_marker_present(text: str, marker: str) -> bool:
    phrase = marker.strip()
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return (
        re.search(
            rf"(?<![A-Za-z0-9_-]){escaped}(?![A-Za-z0-9_-])",
            text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def curate_third_party_skills(vendor: Path, policy_path: Path) -> SkillCuration:
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServingError(f"skill policy is unreadable: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ServingError("skill policy has an unsupported schema")
    allow = policy.get("allow")
    markers = policy.get("network_markers")
    if (
        not isinstance(allow, dict)
        or not isinstance(markers, list)
        or any(not isinstance(marker, str) or not marker.strip() for marker in markers)
    ):
        raise ServingError("skill policy allow/network_markers are invalid")
    allowed: list[CuratedSkill] = []
    flagged: list[CuratedSkill] = []
    excluded: list[CuratedSkill] = []
    layouts = {
        "superpowers": (vendor / "superpowers" / "skills", "sp"),
        "gstack": (vendor / "gstack", "gs"),
    }
    normalized_markers = tuple(marker.strip().lower() for marker in markers)
    for pack, (base, prefix) in layouts.items():
        names = allow.get(pack)
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not PORTABLE_NAME.fullmatch(name)
            for name in names
        ):
            raise ServingError(f"skill policy allow.{pack} is invalid")
        selected = set(names)
        if not base.is_dir():
            raise ServingError(f"skill vendor root is missing: {base}")
        discovered: set[str] = set()
        for directory in sorted(
            (item for item in base.iterdir() if item.is_dir()),
            key=lambda item: item.name,
        ):
            skill_file = directory / "SKILL.md"
            if not skill_file.is_file():
                continue
            discovered.add(directory.name)
            item = CuratedSkill(
                f"{prefix}-{directory.name}",
                directory,
                "",
            )
            if directory.name not in selected:
                excluded.append(
                    CuratedSkill(item.name, item.path, "not in the offline allowlist")
                )
                continue
            try:
                text = skill_file.read_text(encoding="utf-8").lower()
            except (OSError, UnicodeDecodeError) as exc:
                raise ServingError(f"skill is unreadable: {skill_file}: {exc}") from exc
            hits = sorted(
                {
                    marker
                    for marker in normalized_markers
                    if _instruction_marker_present(text, marker)
                }
            )
            if hits:
                flagged.append(
                    CuratedSkill(
                        item.name,
                        item.path,
                        "network-capable instructions: " + ", ".join(hits),
                    )
                )
            else:
                allowed.append(
                    CuratedSkill(item.name, item.path, "offline-curated")
                )
        missing = sorted(selected - discovered)
        if missing:
            raise ServingError(
                f"{pack}: allowlisted skill(s) missing from vendor: "
                + ", ".join(missing)
            )
    return SkillCuration(tuple(allowed), tuple(flagged), tuple(excluded))


def _run_capture(argv: Sequence[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _system_memory_mib() -> tuple[int, int]:
    override = os.environ.get("ORACLE_RESOURCE_SNAPSHOT")
    if override:
        try:
            payload = json.loads(override)
            total = int(payload["system_total_mib"])
            available = int(payload["system_available_mib"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServingError("ORACLE_RESOURCE_SNAPSHOT is invalid") from exc
        if total <= 0 or available <= 0 or available > total:
            raise ServingError("ORACLE_RESOURCE_SNAPSHOT memory is invalid")
        return total, available
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ServingError("GlobalMemoryStatusEx failed")
        return (
            status.total_physical // (1024 * 1024),
            status.available_physical // (1024 * 1024),
        )
    proc_memory = Path("/proc/meminfo")
    if proc_memory.is_file():
        values: dict[str, int] = {}
        for raw in proc_memory.read_text(encoding="ascii").splitlines():
            fields = raw.replace(":", "").split()
            if len(fields) >= 2 and fields[1].isdigit():
                values[fields[0]] = int(fields[1]) // 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        if total > 0 and 0 < available <= total:
            return total, available
    if host_platform.system() == "Darwin":
        total_result = _run_capture(["sysctl", "-n", "hw.memsize"])
        vm_result = _run_capture(["vm_stat"])
        if total_result.returncode == 0 and vm_result.returncode == 0:
            try:
                total = int(total_result.stdout.strip()) // (1024 * 1024)
                page_match = re.search(
                    r"page size of ([0-9]+) bytes", vm_result.stdout
                )
                page_size = int(page_match.group(1)) if page_match else 4096
                pages = 0
                for label in (
                    "Pages free",
                    "Pages inactive",
                    "Pages speculative",
                    "Pages purgeable",
                ):
                    match = re.search(
                        rf"^{re.escape(label)}:\s*([0-9]+)\.",
                        vm_result.stdout,
                        flags=re.MULTILINE,
                    )
                    if match:
                        pages += int(match.group(1))
                available = pages * page_size // (1024 * 1024)
            except (ValueError, AttributeError) as exc:
                raise ServingError("macOS memory evidence is invalid") from exc
            if total > 0 and 0 < available <= total:
                return total, available
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total_pages = os.sysconf("SC_PHYS_PAGES")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        return (
            int(page_size * total_pages // (1024 * 1024)),
            int(page_size * available_pages // (1024 * 1024)),
        )
    except (AttributeError, OSError, ValueError) as exc:
        raise ServingError("available system memory could not be measured") from exc


def _nvidia_smi_output() -> str | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    completed = _run_capture(
        [
            executable,
            "--query-gpu=memory.total,memory.free,name",
            "--format=csv,noheader,nounits",
        ]
    )
    return completed.stdout if completed.returncode == 0 else None


def _macos_wired_limit_mib() -> int:
    completed = _run_capture(["sysctl", "-n", "iogpu.wired_limit_mb"])
    if completed.returncode != 0:
        return 0
    try:
        value = int(completed.stdout.strip())
    except ValueError:
        return 0
    return value if value > 0 else 0


def _vulkan_available() -> bool:
    forced = os.environ.get("ORACLE_VULKAN_AVAILABLE")
    if forced in {"0", "1"}:
        return forced == "1"
    executable = shutil.which("vulkaninfo")
    if not executable:
        return False
    return _run_capture([executable, "--summary"]).returncode == 0


def detect_resources(requested_backend: str = "auto") -> ResourceSnapshot:
    total, available = _system_memory_mib()
    system = host_platform.system()
    nvidia_output = _nvidia_smi_output()
    vulkan = _vulkan_available()
    if system == "Windows":
        snapshot = windows_resource_snapshot(
            total_mib=total,
            available_mib=available,
            nvidia_smi_output=nvidia_output,
            vulkan_available=vulkan,
            adapter_ram_bytes=None,
        )
    elif system == "Darwin":
        wired_limit = _macos_wired_limit_mib()
        snapshot = ResourceSnapshot(
            system_total_mib=total,
            system_available_mib=available,
            backend=Backend.METAL,
            accelerator_total_mib=wired_limit,
            accelerator_available_mib=min(available, wired_limit),
            accelerator_shared=True,
            capability_source=(
                "Metal unified memory; iogpu.wired_limit_mb"
                if wired_limit
                else "Metal unified memory; wired accelerator limit unavailable"
            ),
        )
    elif nvidia_output:
        devices = parse_nvidia_smi(nvidia_output)
        accelerator_total, accelerator_free = aggregate_nvidia_memory(devices)
        snapshot = ResourceSnapshot(
            system_total_mib=total,
            system_available_mib=available,
            backend=Backend.CUDA,
            accelerator_total_mib=accelerator_total,
            accelerator_available_mib=accelerator_free,
            accelerator_shared=False,
            capability_source="nvidia-smi exact MiB",
        )
    elif vulkan:
        snapshot = ResourceSnapshot(
            system_total_mib=total,
            system_available_mib=available,
            backend=Backend.VULKAN,
            capability_source="Vulkan capability; accelerator memory untrusted",
        )
    else:
        snapshot = ResourceSnapshot(
            system_total_mib=total,
            system_available_mib=available,
            backend=Backend.CPU,
            capability_source="CPU fallback",
        )
    if requested_backend.lower() == "auto":
        return snapshot
    available_backends = {Backend.CPU, snapshot.backend}
    if vulkan:
        available_backends.add(Backend.VULKAN)
    if system == "Darwin":
        available_backends.add(Backend.METAL)
    selected = select_backend(
        requested_backend,
        available=available_backends,
    )
    return replace(snapshot, backend=selected)


def _load_admission(path: Path) -> tuple[dict[str, Any], dict[str, ContextPlan]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ServingError("admission metadata is unreadable: file is missing") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServingError(
            f"admission metadata is unreadable: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ServingError("admission metadata has an unsupported schema")
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise ServingError("admission metadata has no models")
    contexts: dict[str, ContextPlan] = {}
    try:
        for name, raw in raw_models.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                raise ServingError("admission metadata contains an invalid model")
            integer_fields = (
                "nominal_context",
                "server_context",
                "parallel_slots",
                "slot_context",
                "advertised_context",
                "prompt_tool_overhead",
                "output_reserve",
                "model_memory_mib",
                "peak_memory_mib",
                "usable_memory_mib",
            )
            if any(
                not isinstance(raw.get(field), int)
                or isinstance(raw.get(field), bool)
                for field in integer_fields
            ):
                raise ServingError("admission metadata model fields are invalid")
            raw_kv = raw.get("kv_mib_per_token")
            if (
                not isinstance(raw_kv, (int, float))
                or isinstance(raw_kv, bool)
                or not math.isfinite(float(raw_kv))
            ):
                raise ServingError("admission metadata model fields are invalid")
            context = ContextPlan(
                model_name=name,
                nominal_context_tokens=raw["nominal_context"],
                parallel_slots=raw["parallel_slots"],
                slot_context_tokens=raw["slot_context"],
                advertised_context_tokens=raw["advertised_context"],
                prompt_tool_overhead_tokens=raw["prompt_tool_overhead"],
                output_reserve_tokens=raw["output_reserve"],
                model_memory_mib=raw["model_memory_mib"],
                kv_mib_per_token=float(raw_kv),
                peak_memory_mib=raw["peak_memory_mib"],
                usable_memory_mib=raw["usable_memory_mib"],
            )
            positive_values = (
                context.nominal_context_tokens,
                context.parallel_slots,
                context.slot_context_tokens,
                context.advertised_context_tokens,
                context.prompt_tool_overhead_tokens,
                context.output_reserve_tokens,
                context.model_memory_mib,
                context.kv_mib_per_token,
                context.peak_memory_mib,
                context.usable_memory_mib,
            )
            expected_server_context = (
                context.slot_context_tokens * context.parallel_slots
            )
            expected_advertised = (
                context.slot_context_tokens
                - context.prompt_tool_overhead_tokens
                - context.output_reserve_tokens
            )
            expected_peak = (
                context.model_memory_mib
                + MODEL_RUNTIME_OVERHEAD_MIB
                + math.ceil(
                    expected_server_context * context.kv_mib_per_token
                )
            )
            if (
                any(value <= 0 for value in positive_values)
                or raw["server_context"] != expected_server_context
                or expected_server_context > context.nominal_context_tokens
                or context.advertised_context_tokens != expected_advertised
                or context.peak_memory_mib != expected_peak
                or context.peak_memory_mib > context.usable_memory_mib
            ):
                raise ServingError(
                    f"{name}: admission metadata has an inconsistent context envelope"
                )
            contexts[name] = context
    except (KeyError, TypeError, ValueError) as exc:
        raise ServingError("admission metadata model fields are invalid") from exc
    return payload, contexts


def _http_transport(
    base_url: str, timeout: int = 300
) -> Callable[[str, str, dict[str, object] | None], HttpResponse]:
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or not _is_loopback_without_raise(parsed.hostname)
        or parsed.username
        or parsed.password
    ):
        raise ServingError("probe endpoint must be credential-free loopback HTTP")
    base = base_url.rstrip("/")

    def transport(
        method: str, path: str, payload: dict[str, object] | None
    ) -> HttpResponse:
        started = time.monotonic()
        data = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _no_redirect_opener().open(request, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024)
                try:
                    body: Any = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {"unparsed_response": raw[:512].decode("utf-8", "replace")}
                return HttpResponse(
                    response.status,
                    body,
                    dict(response.headers.items()),
                    int((time.monotonic() - started) * 1000),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(1024 * 1024)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"error": raw[:512].decode("utf-8", "replace")}
            return HttpResponse(
                exc.code,
                body,
                dict(exc.headers.items()),
                int((time.monotonic() - started) * 1000),
            )
        except (OSError, urllib.error.URLError) as exc:
            return HttpResponse(
                0,
                {"error": type(exc).__name__},
                {},
                int((time.monotonic() - started) * 1000),
            )

    return transport


def inspect_loopback_listeners(port: int) -> tuple[str, ...]:
    if os.name == "nt":
        netstat = _run_capture(["netstat", "-ano", "-p", "tcp"])
        listeners: set[str] = set()
        if netstat.returncode == 0:
            for raw in netstat.stdout.splitlines():
                fields = raw.split()
                if len(fields) < 4 or fields[0].upper() != "TCP":
                    continue
                local = fields[1]
                state = fields[3].upper()
                if state == "LISTENING" and local.rsplit(":", 1)[-1] == str(port):
                    listeners.add(local)
        return tuple(sorted(listeners))
    lsof = shutil.which("lsof")
    if not lsof:
        return ()
    completed = _run_capture(
        [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"]
    )
    listeners = set()
    for raw in completed.stdout.splitlines():
        if raw.startswith("n"):
            value = raw[1:].split("->", 1)[0]
            if value:
                listeners.add(value)
    return tuple(sorted(listeners))


def _headless_engine_runner(
    root: Path, model: str
) -> Callable[[str], tuple[int, str]]:
    prompt = (
        "This is a read-only local engine wiring probe. "
        "Reply with exactly ENGINE-OK."
    )

    def run(engine: str) -> tuple[int, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "ORACLE_ROOT": str(root),
                "UV_OFFLINE": "1",
                "UV_CACHE_DIR": str(root / "incoming" / "dependency-cache" / "uv"),
            }
        )
        if os.name == "nt":
            executable = root / ".tools" / "npm" / f"{engine}.cmd"
            tool_paths = [
                root / ".tools" / "bin",
                root / ".tools" / "npm",
                root / ".tools" / "npm" / "node_modules" / ".bin",
            ]
        else:
            executable = root / ".tools" / "npm" / "bin" / engine
            tool_paths = [
                root / ".tools" / "bin",
                root / ".tools" / "npm" / "bin",
            ]
        if not executable.is_file():
            return 127, "engine executable unavailable"
        environment["PATH"] = os.pathsep.join(
            [*(str(path) for path in tool_paths), environment.get("PATH", "")]
        )
        args = [str(executable)]
        if engine == "claude":
            settings = (
                root / "state" / "generated" / "claude-code" / "settings.json"
            )
            if not settings.is_file():
                return 127, "generated Claude settings unavailable"
            args.extend(
                [
                    "--settings",
                    str(settings),
                    "--mcp-config",
                    str(root / "connectors" / "mcp.claude.json"),
                    "-p",
                    prompt,
                    "--model",
                    model,
                    "--tools",
                    "",
                ]
            )
        elif engine == "opencode":
            config = root / "state" / "generated" / "opencode" / "opencode.json"
            if not config.is_file():
                return 127, "generated OpenCode config unavailable"
            environment["OPENCODE_CONFIG"] = str(config)
            args.extend(["run", "-m", f"oracle/{model}", prompt])
        else:
            return 127, "unsupported engine"
        try:
            with tempfile.TemporaryDirectory(
                prefix="oracle-readonly-engine-probe-"
            ) as temporary:
                scratch = Path(temporary)
                for private_directory in (
                    "claude",
                    "home",
                    "tmp",
                    "xdg-config",
                    "xdg-data",
                    "xdg-cache",
                ):
                    (scratch / private_directory).mkdir()
                environment.update(
                    {
                        "CLAUDE_CONFIG_DIR": str(scratch / "claude"),
                        "HOME": str(scratch / "home"),
                        "USERPROFILE": str(scratch / "home"),
                        "TMPDIR": str(scratch / "tmp"),
                        "TMP": str(scratch / "tmp"),
                        "TEMP": str(scratch / "tmp"),
                        "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
                        "XDG_DATA_HOME": str(scratch / "xdg-data"),
                        "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
                    }
                )
                if engine == "claude":
                    try:
                        settings_payload = json.loads(
                            settings.read_text(encoding="utf-8")
                        )
                    except (
                        OSError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                    ) as exc:
                        return 127, f"generated Claude settings invalid: {type(exc).__name__}"
                    if not isinstance(settings_payload, dict):
                        return 127, "generated Claude settings invalid"
                    settings_payload.pop("hooks", None)
                    settings_payload["permissions"] = {
                        "allow": [],
                        "deny": [
                            "Bash",
                            "Edit",
                            "Glob",
                            "Grep",
                            "NotebookEdit",
                            "Read",
                            "Task",
                            "WebFetch",
                            "WebSearch",
                            "Write",
                        ],
                        "defaultMode": "default",
                    }
                    sanitized_settings = scratch / "claude-settings.json"
                    sanitized_settings.write_text(
                        json.dumps(settings_payload, sort_keys=True) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    empty_mcp = scratch / "mcp.json"
                    empty_mcp.write_text(
                        '{"mcpServers":{}}\n',
                        encoding="utf-8",
                        newline="\n",
                    )
                    args[args.index("--settings") + 1] = str(
                        sanitized_settings
                    )
                    args[args.index("--mcp-config") + 1] = str(empty_mcp)
                elif engine == "opencode":
                    try:
                        opencode_payload = json.loads(
                            config.read_text(encoding="utf-8")
                        )
                    except (
                        OSError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                    ) as exc:
                        return 127, f"generated OpenCode config invalid: {type(exc).__name__}"
                    if not isinstance(opencode_payload, dict):
                        return 127, "generated OpenCode config invalid"
                    opencode_payload["permission"] = {
                        "edit": "deny",
                        "bash": "deny",
                        "webfetch": "deny",
                    }
                    opencode_payload["tools"] = {
                        "bash": False,
                        "edit": False,
                        "write": False,
                        "read": False,
                    }
                    sanitized_opencode = scratch / "opencode.json"
                    sanitized_opencode.write_text(
                        json.dumps(opencode_payload, sort_keys=True) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    environment["OPENCODE_CONFIG"] = str(
                        sanitized_opencode
                    )
                completed = subprocess.run(
                    args,
                    cwd=scratch,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=900,
                    check=False,
                    env=environment,
                )
            return completed.returncode, completed.stdout[-4096:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 124, type(exc).__name__

    return run


def capabilities_payload(root: Path, resources: ResourceSnapshot) -> dict[str, Any]:
    profiles = parse_profiles(
        _read_utf8(root / "serving" / "profiles.conf", "profile declaration")
    )
    models = parse_manifest(
        _read_utf8(root / "serving" / "models.manifest", "model manifest")
    )
    validate_profile_references(profiles, models)
    selected = select_profile(profiles, resources)
    payload: dict[str, Any] = {
        "status": PROVISIONAL,
        "reason": "capability is inferred; no loaded/offloaded backend evidence observed",
        "selected_backend": resources.backend.value,
        "capability_source": resources.capability_source,
        "loaded_backend": None,
        "offloaded_layers": None,
        "usable_capacity_mib": usable_capacity_mib(resources),
        "eligible_profile": selected.name,
        "os_reserve_mib": resources.os_reserve_mib,
        "runtime_reserve_mib": resources.runtime_reserve_mib,
        "accelerator_shared": resources.accelerator_shared,
    }
    admission_path = root / "state" / "generated" / "serving" / "admission.json"
    if admission_path.is_file():
        admission, _ = _load_admission(admission_path)
        payload["admission"] = admission
    return redact_sensitive(payload)


def _service_run(
    *,
    root: Path,
    llama_swap: Path,
    config: Path,
    admission_path: Path,
    gateway_host: str,
    gateway_port: int,
    upstream_host: str,
    upstream_port: int,
) -> int:
    require_loopback(gateway_host)
    require_loopback(upstream_host)
    if not (1 <= gateway_port <= 65535 and 1 <= upstream_port <= 65535):
        raise ServingError("service ports must be between 1 and 65535")
    if gateway_host == upstream_host and gateway_port == upstream_port:
        raise ServingError("gateway and llama-swap ports must differ")
    generated = (root / "state" / "generated" / "serving").resolve()
    if config.parent != generated or admission_path.parent != generated:
        raise ServingError("service config and admission metadata must be generated state")
    if not llama_swap.is_file() or not config.is_file():
        raise ServingError("service binary or generated config is missing")
    evidence, contexts = _load_admission(admission_path)
    expected_config_digest = evidence.get("config_sha256")
    if (
        not isinstance(expected_config_digest, str)
        or not SHA256.fullmatch(expected_config_digest)
    ):
        raise ServingError("admission metadata has no valid config integrity digest")
    try:
        observed_config_digest = _sha256_file(config)
    except OSError as exc:
        raise ServingError("generated config integrity could not be checked") from exc
    if observed_config_digest != expected_config_digest:
        raise ServingError("generated config integrity digest does not match admission")
    if (root / "serving" / "models.manifest").is_file():
        raw_backend = evidence.get("backend")
        selected_backend = (
            str(raw_backend.get("selected_backend", "auto"))
            if isinstance(raw_backend, dict)
            else "auto"
        )
        current_resources = detect_resources(selected_backend)
        evidence, contexts = validate_runtime_freshness(
            root=root,
            config=config,
            admission_path=admission_path,
            llama_swap=llama_swap,
            resources=current_resources,
        )
    raw_models = evidence.get("models")
    big_models = (
        frozenset(
            name
            for name, model in raw_models.items()
            if isinstance(name, str)
            and isinstance(model, dict)
            and model.get("slot") == "big"
        )
        if isinstance(raw_models, dict)
        else frozenset()
    )
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    state = generated
    state.mkdir(parents=True, exist_ok=True)
    command = [
        str(llama_swap),
        "--config",
        str(config),
        "--listen",
        f"{upstream_host}:{upstream_port}",
    ]
    command_digest = hashlib.sha256(
        json.dumps(command, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    started_at = time.time()
    pid_record = PidRecord(
        os.getpid(),
        sys.executable,
        started_at,
        command_digest,
    )
    def inspect_lock_process(pid: int) -> tuple[str, float, str] | None:
        if pid == pid_record.pid:
            return (
                pid_record.executable,
                pid_record.started_at,
                pid_record.command_digest,
            )
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return None
        return ("<live-unverified>", 0.0, "")

    lock_owner = acquire_service_lock(
        state,
        pid_record,
        inspect=inspect_lock_process,
    )
    pid_path = state / "service.pid.json"
    def remove_owned_pid_record() -> None:
        try:
            current = json.loads(pid_path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and current.get("run_token") == lock_owner:
                pid_path.unlink()
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass

    output_handle = None
    error_handle = None
    try:
        atomic_write_text(
            pid_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "pid": pid_record.pid,
                    "executable": pid_record.executable,
                    "started_at": pid_record.started_at,
                    "command_digest": pid_record.command_digest,
                    "run_token": lock_owner,
                    "gateway": f"{gateway_host}:{gateway_port}",
                    "upstream": f"{upstream_host}:{upstream_port}",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        output_handle = (logs / "llama-swap.out.log").open("ab")
        error_handle = (logs / "llama-swap.err.log").open("ab")
    except BaseException:
        if output_handle is not None:
            output_handle.close()
        if error_handle is not None:
            error_handle.close()
        remove_owned_pid_record()
        release_service_lock(state, lock_owner)
        raise
    try:
        child = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=output_handle,
            stderr=error_handle,
        )
    except OSError as exc:
        output_handle.close()
        error_handle.close()
        remove_owned_pid_record()
        release_service_lock(state, lock_owner)
        raise ServingError(f"could not start llama-swap: {type(exc).__name__}") from exc

    def terminate_child() -> None:
        if child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    try:
        server = create_admission_server(
            host=gateway_host,
            port=gateway_port,
            upstream=f"http://{upstream_host}:{upstream_port}",
            contexts=contexts,
            evidence=evidence,
            exclusive_groups=(big_models,) if len(big_models) > 1 else (),
            aliases=(
                {
                    str(alias): str(target)
                    for alias, target in evidence.get("aliases", {}).items()
                }
                if isinstance(evidence.get("aliases"), dict)
                else {}
            ),
        )
    except (OSError, ServingError) as exc:
        terminate_child()
        output_handle.close()
        error_handle.close()
        remove_owned_pid_record()
        release_service_lock(state, lock_owner)
        raise ServingError(
            f"could not start admission gateway: {type(exc).__name__}"
        ) from exc
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)

    def watch_child() -> None:
        child.wait()
        if not stopping.is_set():
            stopping.set()
            server.shutdown()

    watcher = threading.Thread(
        target=watch_child,
        name="oracle-llama-swap-watch",
        daemon=True,
    )
    watcher.start()
    try:
        server.serve_forever(poll_interval=0.25)
        return 0 if child.poll() in {None, 0} else int(child.returncode or 1)
    finally:
        server.server_close()
        terminate_child()
        output_handle.close()
        error_handle.close()
        remove_owned_pid_record()
        release_service_lock(state, lock_owner)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shared read-only serving admission and verification."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser("capabilities")
    capabilities.add_argument("--root", type=Path, required=True)
    capabilities.add_argument(
        "--backend", choices=["auto", *(item.value for item in Backend)], default="auto"
    )

    render = subparsers.add_parser("render")
    render.add_argument("--root", type=Path, required=True)
    render.add_argument("--server", type=Path, required=True)
    render.add_argument("--llama-swap", type=Path)
    render.add_argument("--platform", choices=["posix", "windows"], required=True)
    render.add_argument(
        "--backend", choices=["auto", *(item.value for item in Backend)], default="auto"
    )

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--base-url", default="http://127.0.0.1:9099")
    verify.add_argument("--include-engines", action="store_true")

    safe_output = subparsers.add_parser("safe-output")
    safe_output.add_argument("--value", required=True)

    verify_binary = subparsers.add_parser("verify-binary")
    verify_binary.add_argument("--installed", type=Path, required=True)
    verify_binary.add_argument("--archive", type=Path, required=True)
    verify_binary.add_argument("--member", required=True)

    verify_binary_tree = subparsers.add_parser("verify-binary-tree")
    verify_binary_tree.add_argument("--installed-directory", type=Path, required=True)
    verify_binary_tree.add_argument("--archive", type=Path, required=True)
    verify_binary_tree.add_argument("--anchor-member", required=True)

    skills = subparsers.add_parser("skill-policy")
    skills.add_argument("--vendor", type=Path, required=True)
    skills.add_argument("--policy", type=Path, required=True)
    skills.add_argument("--format", choices=["json", "paths"], default="json")

    launchd = subparsers.add_parser("launchd-plist")
    launchd.add_argument("--output", type=Path, required=True)
    launchd.add_argument("--label", required=True)
    launchd.add_argument("--python", type=Path, required=True)
    launchd.add_argument("--root", type=Path, required=True)
    launchd.add_argument("--llama-swap", type=Path, required=True)
    launchd.add_argument("--config", type=Path, required=True)
    launchd.add_argument("--admission", type=Path, required=True)
    launchd.add_argument("--stdout", type=Path, required=True)
    launchd.add_argument("--stderr", type=Path, required=True)

    service = subparsers.add_parser("service-run")
    service.add_argument("--root", type=Path, required=True)
    service.add_argument("--llama-swap", type=Path, required=True)
    service.add_argument("--config", type=Path, required=True)
    service.add_argument("--admission", type=Path, required=True)
    service.add_argument("--gateway-host", default="127.0.0.1")
    service.add_argument("--gateway-port", type=int, default=9099)
    service.add_argument("--upstream-host", default="127.0.0.1")
    service.add_argument("--upstream-port", type=int, default=9098)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "safe-output":
            print(safe_output_name(args.value))
            return 0
        if args.command == "verify-binary":
            validate_binary_against_archive(
                installed=args.installed.resolve(),
                archive=args.archive.resolve(),
                member_name=args.member,
            )
            print(args.installed.resolve())
            return 0
        if args.command == "verify-binary-tree":
            validate_binary_tree_against_archive(
                installed_directory=args.installed_directory.resolve(),
                archive=args.archive.resolve(),
                anchor_member=args.anchor_member,
            )
            print(args.installed_directory.resolve())
            return 0
        if args.command == "capabilities":
            resources = detect_resources(args.backend)
            print(
                json.dumps(
                    capabilities_payload(args.root.resolve(), resources),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "render":
            resources = detect_resources(args.backend)
            plan = prepare_runtime(
                root=args.root.resolve(),
                server_path=args.server.resolve(),
                llama_swap_path=(
                    args.llama_swap.resolve() if args.llama_swap is not None else None
                ),
                platform=args.platform,
                resources=resources,
                requested_backend=args.backend,
            )
            print(
                json.dumps(
                    {
                        "status": PASS,
                        "profile": plan.profile.name,
                        "backend": plan.backend.value,
                        "config": str(plan.rendered.path),
                        "admission": str(plan.rendered.metadata_path),
                        "models": {
                            name: {
                                "advertised_context": context.advertised_context_tokens,
                                "parallel_slots": context.parallel_slots,
                                "placement": {
                                    "backend": plan.placements[name].backend.value,
                                    "offloaded_layers": (
                                        plan.placements[name].offloaded_layers
                                    ),
                                },
                            }
                            for name, context in sorted(plan.contexts.items())
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "skill-policy":
            result = curate_third_party_skills(
                args.vendor.resolve(), args.policy.resolve()
            )
            if args.format == "paths":
                for item in result.allowed:
                    print(item.path)
            else:
                print(
                    json.dumps(
                        {
                            "status": PROVISIONAL if result.flagged else PASS,
                            "allowed": [
                                {"name": item.name, "path": str(item.path)}
                                for item in result.allowed
                            ],
                            "flagged": [
                                {
                                    "name": item.name,
                                    "path": str(item.path),
                                    "reason": item.reason,
                                }
                                for item in result.flagged
                            ],
                            "excluded": [
                                {"name": item.name, "reason": item.reason}
                                for item in result.excluded
                            ],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 2 if result.flagged else 0
        if args.command == "launchd-plist":
            write_launchd_plist(
                output=args.output.resolve(),
                label=args.label,
                python=args.python.resolve(),
                root=args.root.resolve(),
                llama_swap=args.llama_swap.resolve(),
                config=args.config.resolve(),
                admission=args.admission.resolve(),
                stdout=args.stdout.resolve(),
                stderr=args.stderr.resolve(),
            )
            print(args.output.resolve())
            return 0
        if args.command == "verify":
            root = args.root.resolve()
            admission, contexts = _load_admission(
                root / "state" / "generated" / "serving" / "admission.json"
            )
            preflight_models = admission.get("models")
            if not isinstance(preflight_models, dict):
                raise ServingError("admission metadata has no models")
            preflight_declared = parse_manifest(
                _read_utf8(
                    root / "serving" / "models.manifest",
                    "model manifest",
                )
            )
            preflight_undeclared = sorted(
                set(preflight_models) - set(preflight_declared)
            )
            if preflight_undeclared:
                raise ServingError(
                    "admission metadata references undeclared models: "
                    + ", ".join(preflight_undeclared)
                )
            backend_evidence_payload = admission.get("backend")
            selected_backend = (
                backend_evidence_payload.get("selected_backend")
                if isinstance(backend_evidence_payload, dict)
                else None
            )
            binaries = admission.get("binaries")
            raw_swap = (
                binaries.get("llama_swap")
                if isinstance(binaries, dict)
                else None
            )
            swap_path = (
                Path(str(raw_swap.get("path", "")))
                if isinstance(raw_swap, dict)
                else Path()
            )
            if (
                not isinstance(selected_backend, str)
                or selected_backend not in {backend.value for backend in Backend}
                or not isinstance(raw_swap, dict)
            ):
                raise ServingError(
                    "admission metadata has incomplete runtime provenance"
                )
            admission, contexts = validate_runtime_freshness(
                root=root,
                config=(
                    root
                    / "state"
                    / "generated"
                    / "serving"
                    / "llama-swap.yaml"
                ),
                admission_path=(
                    root
                    / "state"
                    / "generated"
                    / "serving"
                    / "admission.json"
                ),
                llama_swap=swap_path,
                resources=detect_resources(selected_backend),
            )
            tiers = admission.get("tiers")
            if not isinstance(tiers, dict):
                raise ServingError("admission metadata has no tier mapping")
            raw_models = admission.get("models")
            if not isinstance(raw_models, dict):
                raise ServingError("admission metadata has no models")
            admitted_names = set(contexts)
            declared_models = parse_manifest(
                _read_utf8(
                    root / "serving" / "models.manifest",
                    "model manifest",
                )
            )
            undeclared = sorted(admitted_names - set(declared_models))
            if undeclared:
                raise ServingError(
                    "admission metadata references undeclared models: "
                    + ", ".join(undeclared)
                )
            if any(
                not isinstance(tiers.get(key), str) or not tiers.get(key)
                for key in TIER_KEYS
            ):
                raise ServingError("admission metadata has an incomplete tier mapping")
            tier_values = {str(tiers[key]) for key in TIER_KEYS}
            if not tier_values.issubset(admitted_names):
                raise ServingError(
                    "admission tier mapping references a model outside the plan"
                )
            chat_model = str(tiers.get("HAIKU_MODEL", ""))
            embedding_model = next(
                (
                    name
                    for name in raw_models
                    if isinstance(name, str)
                    and declared_models[name].slot == "embed"
                ),
                "",
            )
            if not chat_model or not embedding_model:
                raise ServingError("admission metadata has no chat/embedding model")
            # Verification loads real models and runs inference, which can take
            # minutes. Stream step names plus an elapsed-time heartbeat to
            # stderr so it never looks frozen; stdout stays pure JSON for
            # callers that parse it.
            def _verify_progress(message: str) -> None:
                print(f"==> {message}", file=sys.stderr, flush=True)

            verify_started = time.monotonic()
            verify_done = threading.Event()

            def _verify_heartbeat() -> None:
                while not verify_done.wait(5):
                    elapsed = int(time.monotonic() - verify_started)
                    print(
                        f"    ... still verifying ({elapsed}s elapsed)",
                        file=sys.stderr,
                        flush=True,
                    )

            heartbeat = threading.Thread(target=_verify_heartbeat, daemon=True)
            heartbeat.start()
            try:
                results = run_offline_probes(
                    transport=_http_transport(args.base_url),
                    contexts=contexts,
                    chat_model=chat_model,
                    embedding_model=embedding_model,
                    listeners=(),
                    listener_inspector=inspect_loopback_listeners,
                    expected_listener_ports={"public": 9099, "internal": 9098},
                    planned_placements={
                        name: raw["placement"]
                        for name, raw in raw_models.items()
                        if isinstance(name, str)
                        and isinstance(raw, dict)
                        and isinstance(raw.get("placement"), dict)
                    },
                    engine_runner=(
                        _headless_engine_runner(root, chat_model)
                        if args.include_engines
                        else None
                    ),
                    progress=_verify_progress,
                )
            finally:
                verify_done.set()
                heartbeat.join(timeout=1)
            aggregate = aggregate_probe_status(results)
            print(
                json.dumps(
                    {
                        "status": aggregate,
                        "profile": admission.get("profile"),
                        "results": [
                            {
                                "name": item.name,
                                "status": item.status,
                                "reason": item.reason,
                                "evidence": item.evidence,
                            }
                            for item in results
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if aggregate == PASS else 2 if aggregate == PROVISIONAL else 1
        if args.command == "service-run":
            return _service_run(
                root=args.root.resolve(),
                llama_swap=args.llama_swap.resolve(),
                config=args.config.resolve(),
                admission_path=args.admission.resolve(),
                gateway_host=args.gateway_host,
                gateway_port=args.gateway_port,
                upstream_host=args.upstream_host,
                upstream_port=args.upstream_port,
            )
        raise ServingError(f"unsupported command: {args.command}")
    except ServingError as exc:
        if args.command == "verify":
            print(
                json.dumps(
                    redact_sensitive(
                        {
                            "status": FAIL,
                            "profile": None,
                            "results": [
                                {
                                    "name": "runtime_admission",
                                    "status": FAIL,
                                    "reason": str(exc),
                                    "evidence": {},
                                }
                            ],
                        }
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
