#!/usr/bin/env python3
"""Fail-closed stdio guard for the pinned LeanCTX MCP server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ALLOWED_TOOLS = frozenset(
    {"ctx_read", "ctx_search", "ctx_glob", "ctx_tree", "ctx_shell"}
)
ALLOWED_SHELL_COMMANDS = frozenset(
    {
        "pwd",
        "ls",
        "dir",
    }
)
SERVER_INSTRUCTIONS = """\
LeanCTX provides five compact, local tools: ctx_read, ctx_search, ctx_glob,
ctx_tree, and ctx_shell. Prefer the first four for routine exploration; ctx_shell
accepts only argument-free pwd, ls, or dir inspection. Native tools remain valid
and are required for all commands with options or paths, builds, tests, Git,
interpreters, package managers, exact failure evidence, security artifacts,
release manifests, and production-shaped probes. No other LeanCTX tools or
network, setup, update, cloud, proxy, daemon, or publish operations are available."""


def sanitize_server_message(message: Any) -> Any:
    """Replace upstream instructions and filter advertised tools."""

    if not isinstance(message, dict):
        return message
    result = message.get("result")
    if not isinstance(result, dict):
        return message
    if isinstance(result.get("serverInfo"), dict):
        result["instructions"] = SERVER_INSTRUCTIONS
    tools = result.get("tools")
    if isinstance(tools, list):
        result["tools"] = [
            tool
            for tool in tools
            if isinstance(tool, dict) and tool.get("name") in ALLOWED_TOOLS
        ]
    return message


def blocked_tool_response(message: Any) -> dict[str, Any] | None:
    """Return an MCP tool error when a hidden or unsafe tool call is requested."""

    if not isinstance(message, Mapping) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    tool_name = params.get("name") if isinstance(params, Mapping) else None
    arguments = params.get("arguments") if isinstance(params, Mapping) else None
    reason = None
    if tool_name not in ALLOWED_TOOLS:
        reason = f"tool is disabled: {tool_name}"
    elif tool_name == "ctx_shell":
        command = arguments.get("command") if isinstance(arguments, Mapping) else None
        if not _shell_command_allowed(command):
            reason = "ctx_shell accepts only one allowlisted local inspection command"
    if reason is None:
        return None
    request_id = message.get("id")
    if request_id is None:
        return {}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": f"LeanCTX call blocked by SentiVue Oracle: {reason}",
                }
            ],
            "isError": True,
        },
    }


def _shell_command_allowed(command: Any) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if any(token in command for token in ("&", "|", ";", ">", "<", "`", "$(", "\n", "\r")):
        return False
    match = re.match(r"""^\s*(?:"([^"]+)"|'([^']+)'|([^\s]+))""", command)
    if match is None:
        return False
    executable = next(group for group in match.groups() if group is not None)
    if command[match.end() :].strip():
        return False
    if "/" in executable or "\\" in executable:
        return False
    executable = executable.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
            break
    if executable not in ALLOWED_SHELL_COMMANDS:
        return False
    return True


def _linked_checkout_root(project_root: Path) -> Path | None:
    marker = project_root / ".git"
    if not marker.is_file():
        return None
    try:
        first_line = marker.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeError, IndexError):
        return None
    prefix = "gitdir:"
    if not first_line.lower().startswith(prefix):
        return None
    git_dir = Path(first_line[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (project_root / git_dir).resolve()
    if git_dir.parent.name.lower() != "worktrees":
        return None
    return git_dir.parent.parent.parent


def _lean_ctx_runtime() -> tuple[str, dict[str, str]]:
    roots: list[Path] = []
    for value in (
        os.environ.get("ORACLE_ROOT"),
        os.environ.get("LEAN_CTX_PROJECT_ROOT"),
    ):
        if value:
            roots.append(Path(value).resolve())
    project_root = os.environ.get("LEAN_CTX_PROJECT_ROOT")
    if project_root:
        linked_root = _linked_checkout_root(Path(project_root).resolve())
        if linked_root is not None:
            roots.append(linked_root)

    seen: set[Path] = set()
    binary_name = "lean-ctx.exe" if os.name == "nt" else "lean-ctx"
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        binary = root / ".tools" / "bin" / binary_name
        if not binary.is_file():
            continue
        template = root / "engines" / "shared" / "lean-ctx-config.toml"
        policy = root / "state" / "lean-ctx" / "config" / "config.toml"
        try:
            policy_matches = template.read_bytes() == policy.read_bytes()
        except OSError:
            policy_matches = False
        if not policy_matches:
            raise RuntimeError(
                "repo-local LeanCTX policy is missing or differs; rerun platform setup"
            )
        child_env = dict(os.environ)
        child_env["ORACLE_ROOT"] = str(root)
        child_env["LEAN_CTX_CONFIG_DIR"] = str(policy.parent)
        return str(binary), child_env
    raise RuntimeError(
        "policy-bound lean-ctx is missing; run the policy-bound platform setup"
    )


def main() -> int:
    try:
        command, child_env = _lean_ctx_runtime()
    except RuntimeError as exc:
        print(f"lean-ctx MCP guard: {exc}", file=sys.stderr)
        return 127

    child = subprocess.Popen(
        [command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )
    if child.stdin is None or child.stdout is None or child.stderr is None:
        child.kill()
        print("lean-ctx MCP guard: failed to open child pipes", file=sys.stderr)
        return 1

    output_lock = threading.Lock()

    def emit(message: Any) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with output_lock:
            sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()

    def forward_input() -> None:
        try:
            for raw_line in sys.stdin.buffer:
                try:
                    message = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    child.stdin.write(raw_line)
                    child.stdin.flush()
                    continue
                blocked = blocked_tool_response(message)
                if blocked is not None:
                    if blocked:
                        emit(blocked)
                    continue
                child.stdin.write(raw_line)
                child.stdin.flush()
        finally:
            child.stdin.close()

    def forward_stderr() -> None:
        for chunk in iter(lambda: child.stderr.read(8192), b""):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    input_thread = threading.Thread(target=forward_input, daemon=True)
    stderr_thread = threading.Thread(target=forward_stderr, daemon=True)
    input_thread.start()
    stderr_thread.start()
    try:
        for raw_line in child.stdout:
            try:
                message = sanitize_server_message(json.loads(raw_line))
            except (UnicodeDecodeError, json.JSONDecodeError):
                with output_lock:
                    sys.stdout.buffer.write(raw_line)
                    sys.stdout.buffer.flush()
            else:
                emit(message)
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
