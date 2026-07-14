#!/usr/bin/env python3
"""Resolve Hugging Face revisions and LFS hashes into reviewable model policy."""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
)

# Trusted per-model transformer layer counts are sourced from each model's
# OFFICIAL BASE repository config.json (num_hidden_layers == GGUF block_count),
# never from the downloaded GGUF bytes. The GGUF re-quantization repos carry no
# config.json, so the base repos below are the independent authority. This map
# is pinned and explicit on purpose: an unmapped model fails closed rather than
# guessing its layer count.
LAYER_AUTHORITY_BASE_REPOS: dict[str, str] = {
    "deepseek-v3.2": "deepseek-ai/DeepSeek-V3.2-Exp",
    "kimi-k2-thinking": "moonshotai/Kimi-K2-Thinking",
    "qwen3-coder-480b": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
    "qwen3-coder-30b": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "qwen3-coder-30b-q4": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "qwen2.5-coder-7b": "Qwen/Qwen2.5-Coder-7B-Instruct",
    "qwen3-embedding-4b": "Qwen/Qwen3-Embedding-4B",
}


class ResolutionError(RuntimeError):
    """A remote model identity could not be resolved safely."""


def _request_json(url: str, token: str | None) -> tuple[Any, str | None]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "sentivue-oracle-model-authority/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
            link = response.headers.get("Link")
    except Exception as exc:
        raise ResolutionError(f"request failed for {url}: {exc}") from exc
    next_url = None
    if link:
        for item in link.split(","):
            match = re.match(r'\s*<([^>]+)>;\s*rel="next"\s*$', item)
            if match:
                next_url = match.group(1)
                break
    return payload, next_url


def _request_text(url: str, token: str | None) -> str:
    headers = {"User-Agent": "sentivue-oracle-model-authority/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        raise ResolutionError(f"request failed for {url}: {exc}") from exc


def _verify_lfs_pointer(
    repository: str,
    revision: str,
    path: str,
    sha256: str,
    size: int,
    token: str | None,
) -> None:
    encoded_repository = urllib.parse.quote(repository, safe="/")
    encoded_revision = urllib.parse.quote(revision, safe="")
    encoded_path = urllib.parse.quote(path, safe="/")
    pointer = _request_text(
        f"https://huggingface.co/{encoded_repository}/raw/"
        f"{encoded_revision}/{encoded_path}",
        token,
    )
    expected = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{sha256}\n"
        f"size {size}\n"
    )
    if pointer.replace("\r\n", "\n") != expected:
        raise ResolutionError(
            f"{repository}@{revision}:{path}: raw LFS pointer disagrees with tree API"
        )


def _manifest_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = [field.strip() for field in raw.split("|")]
        if len(fields) != 7:
            raise ResolutionError(
                f"{path}:{line_number}: expected seven pipe-separated fields"
            )
        name, repository, include, *_rest, requested = fields
        if not name or not REPOSITORY_RE.fullmatch(repository) or not include:
            raise ResolutionError(f"{path}:{line_number}: invalid model declaration")
        rows.append(
            {
                "name": name,
                "repository": repository,
                "include": include,
                "requested": requested,
            }
        )
    if not rows:
        raise ResolutionError("model manifest has no entries")
    return rows


def _repo_metadata(repository: str, token: str | None) -> dict[str, Any]:
    encoded = urllib.parse.quote(repository, safe="/")
    payload, _next = _request_json(
        f"https://huggingface.co/api/models/{encoded}", token
    )
    if not isinstance(payload, dict):
        raise ResolutionError(f"{repository}: model metadata is not an object")
    revision = payload.get("sha")
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision):
        raise ResolutionError(f"{repository}: API did not return an immutable revision")
    if payload.get("private") is True:
        raise ResolutionError(f"{repository}: private models cannot ship unattended")
    if payload.get("gated") not in (False, None):
        raise ResolutionError(f"{repository}: gated models cannot ship unattended")
    return {"revision": revision}


def _base_repo_layer_count(
    repository: str, revision: str, token: str | None
) -> int:
    """Read num_hidden_layers from a base repo config.json at a pinned commit."""

    encoded_repository = urllib.parse.quote(repository, safe="/")
    encoded_revision = urllib.parse.quote(revision, safe="")
    raw = _request_text(
        f"https://huggingface.co/{encoded_repository}/resolve/"
        f"{encoded_revision}/config.json",
        token,
    )
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResolutionError(
            f"{repository}@{revision}: config.json is not valid JSON: {exc}"
        ) from exc
    layers = config.get("num_hidden_layers") if isinstance(config, dict) else None
    if not isinstance(layers, int) or isinstance(layers, bool) or layers <= 0:
        raise ResolutionError(
            f"{repository}@{revision}: config.json lacks a positive "
            "num_hidden_layers"
        )
    return layers


def _repo_files(
    repository: str, revision: str, token: str | None
) -> list[dict[str, Any]]:
    encoded_repository = urllib.parse.quote(repository, safe="/")
    encoded_revision = urllib.parse.quote(revision, safe="")
    next_url: str | None = (
        f"https://huggingface.co/api/models/{encoded_repository}/tree/"
        f"{encoded_revision}?recursive=true&expand=true"
    )
    files: list[dict[str, Any]] = []
    while next_url:
        payload, next_url = _request_json(next_url, token)
        if not isinstance(payload, list):
            raise ResolutionError(f"{repository}: tree response is not a list")
        files.extend(item for item in payload if isinstance(item, dict))
    return files


def resolve_authorities(root: Path, token: str | None) -> dict[str, Any]:
    rows = _manifest_rows(root / "serving" / "models.manifest")
    cache: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    layer_cache: dict[str, int] = {}
    models: dict[str, Any] = {}
    for row in rows:
        repository = row["repository"]
        if repository not in cache:
            metadata = _repo_metadata(repository, token)
            revision = str(metadata["revision"])
            cache[repository] = (
                revision,
                _repo_files(repository, revision, token),
            )
        revision, remote_files = cache[repository]
        selected: list[dict[str, Any]] = []
        for item in remote_files:
            path = item.get("path")
            if (
                item.get("type") != "file"
                or not isinstance(path, str)
                or not fnmatch.fnmatchcase(path, row["include"])
            ):
                continue
            size = item.get("size")
            lfs = item.get("lfs")
            oid = lfs.get("oid") if isinstance(lfs, dict) else None
            if (
                not isinstance(size, int)
                or size <= 0
                or not isinstance(oid, str)
                or not SHA256_RE.fullmatch(oid)
            ):
                raise ResolutionError(
                    f"{repository}@{revision}:{path}: missing LFS SHA-256 metadata"
                )
            _verify_lfs_pointer(
                repository,
                revision,
                path,
                oid,
                size,
                token,
            )
            selected.append({"path": path, "sha256": oid, "size": size})
        if not selected:
            raise ResolutionError(
                f"{row['name']}: pattern {row['include']!r} matched no LFS files"
            )
        base_repo = LAYER_AUTHORITY_BASE_REPOS.get(row["name"])
        if base_repo is None:
            raise ResolutionError(
                f"{row['name']}: no pinned base repository for trusted layer "
                "metadata; add it to LAYER_AUTHORITY_BASE_REPOS"
            )
        if base_repo not in layer_cache:
            base_revision = str(_repo_metadata(base_repo, token)["revision"])
            layer_cache[base_repo] = _base_repo_layer_count(
                base_repo, base_revision, token
            )
        layer_count = layer_cache[base_repo]
        total_size = sum(int(item["size"]) for item in selected)
        model_size_mib = max(1, math.ceil(total_size / (1024 * 1024)))
        per_layer_mib = max(1, math.ceil(model_size_mib / layer_count))
        models[row["name"]] = {
            "repository": repository,
            "revision": revision,
            "include": row["include"],
            "files": sorted(selected, key=lambda item: item["path"]),
            "layer_mib": [per_layer_mib] * layer_count,
        }
    return {"schema_version": 1, "models": dict(sorted(models.items()))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    tracked = root / "serving" / "model-authorities.json"
    output = args.output.resolve()
    if output == tracked.resolve():
        raise ResolutionError(
            "write a separate review artifact, then promote it deliberately"
        )
    payload = resolve_authorities(root, os.environ.get("HF_TOKEN"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote review candidate: {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ResolutionError as exc:
        print(f"model authority resolution failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
