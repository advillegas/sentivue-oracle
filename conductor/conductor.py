#!/usr/bin/env python3
"""
SentiVue Oracle conductor â€” the self-governing mission loop.

Contract (see README):
  Automations   mission TOML: goal, tasks, dependencies, acceptance criteria â€” or a
                bare goal with auto_plan=true (the planner decomposes it, and replans
                once if the plan stalls)
  Worktrees     every task runs in an isolated git worktree; merge only after audit
  Verification  layered: deterministic `checks` (conductor-run, exit code 0) ->
                sonnet-tier auditor -> opus-tier tiebreak when checks and auditor
                disagree -> optional adversary pass
  Escalation    the final attempt of a failing task is auto-escalated to the opus tier
  Supervision   total timeout AND output-stall detection kill runaway runs early
  Memory        plain-text memory/LEDGER.md + STATE.md + FAILURES.md; per-attempt
                FEEDBACK.md handoffs inside the worktree
  Throughput    optional second worker drives haiku-tier tasks on the always-resident
                fast lane in parallel with the big slot (workers = 2)
  Reports       hourly REPORT-*.md + FINAL-REPORT.md

Usage:
  uv run --project env python conductor/conductor.py run conductor/missions/example.toml \
      --engine claude --hours 24
Stdlib only (Python >= 3.11 for tomllib).
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MEMORY, REPORTS, LOGS = ROOT / "memory", ROOT / "reports", ROOT / "logs"
WORKTREES = ROOT / ".worktrees"
SWAP_HEALTH = "http://127.0.0.1:9099/health"


class ConductorError(RuntimeError):
    """A fail-closed mission, isolation, checkpoint, or verification error."""


def _fsync_parent(path: Path) -> None:
    """Best-effort directory sync after publishing durable state."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Publish UTF-8 text atomically and durably in the destination directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def append_jsonl_durable(path: Path, record: dict[str, Any]) -> None:
    """Append one complete JSON record and force it to stable storage."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("durable append made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

# Default tier map; overridden by serving/tiers.env (written by ./install so that
# reduced model profiles remap opus/sonnet onto models that actually exist).
TIER_MODEL = {"opus": "kimi-k2-thinking", "sonnet": "qwen3-coder-480b", "haiku": "qwen3-coder-30b"}
_tiers_file = ROOT / "serving" / "tiers.env"
if _tiers_file.exists():
    for _line in _tiers_file.read_text(encoding="utf-8", errors="replace").splitlines():
        _k, _, _v = _line.partition("=")
        _tier = _k.strip().removesuffix("_MODEL").lower()
        if _tier in TIER_MODEL and _v.strip():
            TIER_MODEL[_tier] = _v.strip()


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


IS_WIN = os.name == "nt"
_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[int, subprocess.Popen[str]] = {}


def _assign_windows_kill_job(process: subprocess.Popen[str]) -> int | None:
    """Assign a child to a kill-on-close Job Object when Windows permits it."""

    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job,
        wintypes.HANDLE(process._handle),  # type: ignore[attr-defined]
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_handle(handle: int | None) -> None:
    if not handle:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate the owned process group/tree and confirm the parent exits."""

    if process.poll() is not None:
        return
    if IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise ConductorError(
                f"could not terminate process tree rooted at pid {process.pid}"
            ) from exc


def _terminate_active_processes() -> None:
    with _ACTIVE_PROCESS_LOCK:
        processes = list(_ACTIVE_PROCESSES.values())
    failures: list[str] = []
    for process in processes:
        try:
            _terminate_process_tree(process)
        except (ConductorError, OSError) as exc:
            failures.append(f"{process.pid}: {exc}")
    if failures:
        raise ConductorError(
            "could not terminate active engine process tree(s): "
            + "; ".join(failures)
        )


def sh(args: list[str], cwd: Path | None = None, timeout: int | None = None,
       env: dict | None = None, stall_timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command in its own session/process-group with a total timeout AND an
    optional output-stall timeout (kill when the process goes silent for too long â€”
    catches hung runs long before the total budget is burned)."""
    full_env = {**os.environ, **(env or {})}
    kwargs: dict = {}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    p = subprocess.Popen(args, cwd=cwd, env=full_env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", **kwargs)
    windows_job = _assign_windows_kill_job(p)
    if IS_WIN and windows_job is None:
        _terminate_process_tree(p)
        raise ConductorError(
            "Windows Job Object assignment failed; refusing an unowned process tree"
        )
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES[p.pid] = p
    chunks: list[str] = []
    last_activity = time.monotonic()

    def reader() -> None:
        nonlocal last_activity
        assert p.stdout is not None
        for line in p.stdout:
            chunks.append(line)
            last_activity = time.monotonic()

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        started = time.monotonic()
        killed = ""
        while p.poll() is None:
            t = time.monotonic()
            if timeout and t - started > timeout:
                killed = "[WATCHDOG] killed: total timeout"
                break
            if stall_timeout and t - last_activity > stall_timeout:
                killed = "[WATCHDOG] killed: stalled (no output)"
                break
            time.sleep(0.1)
        if killed:
            try:
                _close_windows_handle(windows_job)
                windows_job = None
                _terminate_process_tree(p)
            except (ProcessLookupError, PermissionError, OSError) as exc:
                raise ConductorError(
                    f"failed to terminate timed-out process tree: {exc}"
                ) from exc
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise ConductorError(
                f"process {p.pid} remained alive after termination"
            ) from exc
        if windows_job is not None:
            _close_windows_handle(windows_job)
            windows_job = None
        elif not IS_WIN:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                time.sleep(0.1)
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        th.join(timeout=5)
        if th.is_alive():
            raise ConductorError(
                f"process tree rooted at pid {p.pid} kept output handles open"
            )
        out = "".join(chunks) + (f"\n{killed}" if killed else "")
        return subprocess.CompletedProcess(
            args,
            -9 if killed else p.returncode,
            out,
            "",
        )
    finally:
        try:
            if windows_job is not None:
                _close_windows_handle(windows_job)
                windows_job = None
            if p.poll() is None:
                _terminate_process_tree(p)
            th.join(timeout=5)
        finally:
            with _ACTIVE_PROCESS_LOCK:
                _ACTIVE_PROCESSES.pop(p.pid, None)


def git(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return sh(["git", "-C", str(repo), *args], timeout=timeout)


def rel_to_root(p: Path) -> str:
    """Path for human display: relative to the repo when possible (memory and
    report dirs may live elsewhere, e.g. under test harnesses)."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _pid_alive(pid: int) -> bool:
    """NB: os.kill(pid, 0) is NOT a liveness probe on Windows (it terminates)."""
    if IS_WIN:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return re.search(rf',"{pid}",', r.stdout or "") is not None
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_CONDUCTOR_LOCK_HANDLE: Any | None = None


def acquire_singleton() -> None:
    """Acquire one OS-backed conductor lock without stale-file races."""

    global _CONDUCTOR_LOCK_HANDLE
    if _CONDUCTOR_LOCK_HANDLE is not None:
        return
    lock = ROOT / "state" / "conductor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("r+b") if lock.exists() else lock.open("w+b")
    if lock.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if IS_WIN:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise SystemExit(
            "REFUSED: another conductor owns the mission lock"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "started_at": time.time(),
            },
            sort_keys=True,
        ).encode("ascii")
    )
    handle.flush()
    os.fsync(handle.fileno())
    _CONDUCTOR_LOCK_HANDLE = handle

    def _release() -> None:
        global _CONDUCTOR_LOCK_HANDLE
        owned = _CONDUCTOR_LOCK_HANDLE
        if owned is None:
            return
        try:
            owned.seek(0)
            if IS_WIN:
                import msvcrt

                msvcrt.locking(owned.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(owned.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        owned.close()
        _CONDUCTOR_LOCK_HANDLE = None

    atexit.register(_release)


# ---------------------------------------------------------------- mission spec

TASK_FIELDS = {"id", "title", "prompt", "depends_on", "acceptance", "checks", "tier",
               "audit_tier", "timeout_minutes", "stall_minutes", "max_attempts",
               "adversary", "escalate", "background", "requires_approval", "research",
               "best_of_n"}
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _string_list(
    value: Any,
    *,
    field_name: str,
    allow_empty: bool,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        qualifier = "non-empty " if not allow_empty else ""
        raise ConductorError(
            f"task {field_name} must be a {qualifier}list of non-empty strings"
        )
    return list(value)


def _strict_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ConductorError(
            f"task {field_name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)   # shell commands; conductor-run gates
    tier: str = "sonnet"
    audit_tier: str = "sonnet"       # verifier should be at least as strong as generator
    timeout_minutes: int = 45
    stall_minutes: int = 20          # output-silence kill; CPU prefill of a large
                                     # context emits nothing for many minutes (L7)
    max_attempts: int = 3
    adversary: bool = False          # extra adversarial pass after audit
    escalate: bool = True            # final attempt auto-escalates to opus tier
    background: bool = False         # only runs when nothing else is dispatchable
    requires_approval: bool = False  # waits for the exact run/task/nonce challenge
    research: bool = False           # read-only researcher pass feeds the first attempt
    best_of_n: int = 1               # frontier resampling: N independent candidates on
                                     # attempt 1; checks+audit pick the winner (max 3)
    # runtime state
    status: str = "pending"          # pending|claimed|running|auditing|done|failed|blocked|merge-conflict
    attempts: int = 0
    infra_strikes: int = 0
    note: str = ""

    @staticmethod
    def from_dict(d: dict, background: bool = False) -> "Task":
        if not isinstance(d, dict):
            raise ConductorError("task declaration must be an object")
        unknown = sorted(set(d) - TASK_FIELDS)
        if unknown:
            raise ConductorError(
                "task declaration has unknown field(s): " + ", ".join(unknown)
            )
        clean = dict(d)
        for required in ("id", "title", "prompt"):
            value = clean.get(required)
            if not isinstance(value, str) or not value.strip():
                raise ConductorError(f"task {required} must be a non-empty string")
        if not TASK_ID_PATTERN.fullmatch(clean["id"]):
            raise ConductorError(f"task id is invalid: {clean['id']!r}")
        clean["depends_on"] = _string_list(
            clean.get("depends_on", []),
            field_name="depends_on",
            allow_empty=True,
        )
        if any(not TASK_ID_PATTERN.fullmatch(item) for item in clean["depends_on"]):
            raise ConductorError("task dependency id is invalid")
        clean["acceptance"] = _string_list(
            clean.get("acceptance", []),
            field_name="acceptance",
            allow_empty=False,
        )
        clean["checks"] = _string_list(
            clean.get("checks", []),
            field_name="checks",
            allow_empty=False,
        )
        for field_name, default, minimum, maximum in (
            ("timeout_minutes", 45, 1, 120),
            ("stall_minutes", 20, 1, 120),
            ("max_attempts", 3, 1, 10),
            ("best_of_n", 1, 1, 3),
        ):
            clean[field_name] = _strict_int(
                clean.get(field_name, default),
                field_name=field_name,
                minimum=minimum,
                maximum=maximum,
            )
        for field_name, default in (
            ("adversary", False),
            ("escalate", True),
            ("background", False),
            ("requires_approval", False),
            ("research", False),
        ):
            value = clean.get(field_name, default)
            if not isinstance(value, bool):
                raise ConductorError(f"task {field_name} must be boolean")
            clean[field_name] = value
        clean["background"] = background or clean["background"]
        for field_name, default in (("tier", "sonnet"), ("audit_tier", "sonnet")):
            value = clean.get(field_name, default)
            if value not in TIER_MODEL:
                raise ConductorError(f"task {field_name} is invalid: {value!r}")
            clean[field_name] = value
        return Task(**clean)


@dataclass
class Mission:
    name: str
    goal: str
    repo: Path
    tasks: list[Task]
    engine: str = "claude"
    hours: float = 24.0
    report_minutes: int = 60
    auto_plan: bool = False          # decompose the goal into tasks at mission start
    workers: int = 1                 # 2 = second worker drives haiku tasks in parallel

    @staticmethod
    def load(path: Path, engine: str | None, hours: float | None) -> "Mission":
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConductorError(f"mission file is invalid: {exc}") from exc
        unknown_top_level = sorted(set(raw) - {"mission", "tasks", "background"})
        if unknown_top_level:
            raise ConductorError(
                "mission file has unknown table(s): "
                + ", ".join(unknown_top_level)
            )
        m = raw.get("mission", {})
        if not isinstance(m, dict):
            raise ConductorError("[mission] must be a table")
        unknown_mission = sorted(
            set(m)
            - {
                "name",
                "goal",
                "repo",
                "engine",
                "hours",
                "report_minutes",
                "auto_plan",
                "workers",
            }
        )
        if unknown_mission:
            raise ConductorError(
                "mission has unknown field(s): " + ", ".join(unknown_mission)
            )
        raw_tasks = raw.get("tasks", [])
        raw_background = raw.get("background", [])
        if not isinstance(raw_tasks, list) or not isinstance(raw_background, list):
            raise ConductorError("tasks and background must be arrays of tables")
        tasks = [Task.from_dict(t) for t in raw_tasks]
        tasks += [
            Task.from_dict(t, background=True)
            for t in raw_background
        ]
        raw_repo = m.get("repo", str(ROOT))
        if not isinstance(raw_repo, str) or not raw_repo.strip():
            raise ConductorError("mission repo must be a non-empty path string")
        repo = Path(os.path.expanduser(raw_repo)).resolve()
        mission = Mission(
            name=m.get("name", path.stem),
            goal=m.get("goal", ""),
            repo=repo,
            tasks=tasks,
            engine=engine or m.get("engine", "claude"),
            hours=hours if hours is not None else m.get("hours", 24),
            report_minutes=m.get("report_minutes", 60),
            auto_plan=m.get("auto_plan", False),
            workers=m.get("workers", 1),
        )
        validate_mission(mission)
        return mission


def _validate_task_dag(tasks: list[Task]) -> None:
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ConductorError("mission has duplicate task ids")
    known = set(ids)
    for task in tasks:
        for dependency in task.depends_on:
            if dependency == task.id:
                raise ConductorError(f"task {task.id} depends on itself")
            if dependency not in known:
                raise ConductorError(
                    f"task {task.id} depends on unknown task {dependency}"
                )
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {task.id: task for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ConductorError("task dependency graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)


def validate_mission(mission: Mission) -> None:
    """Validate programmatic and TOML missions with identical strict rules."""

    if (
        not isinstance(mission.name, str)
        or not TASK_ID_PATTERN.fullmatch(mission.name)
    ):
        raise ConductorError("mission name must be a lowercase slug")
    if not isinstance(mission.goal, str) or not mission.goal.strip():
        raise ConductorError("mission goal must be a non-empty string")
    if mission.engine not in {"claude", "opencode", "kilo"}:
        raise ConductorError(f"mission engine is invalid: {mission.engine!r}")
    if (
        not isinstance(mission.hours, (int, float))
        or isinstance(mission.hours, bool)
        or not 0 < float(mission.hours) <= 720
    ):
        raise ConductorError("mission hours must be in (0, 720]")
    if (
        not isinstance(mission.report_minutes, int)
        or isinstance(mission.report_minutes, bool)
        or not 1 <= mission.report_minutes <= 1440
    ):
        raise ConductorError("mission report_minutes must be in [1, 1440]")
    if not isinstance(mission.auto_plan, bool):
        raise ConductorError("mission auto_plan must be boolean")
    if (
        not isinstance(mission.workers, int)
        or isinstance(mission.workers, bool)
        or mission.workers not in {1, 2}
    ):
        raise ConductorError("mission workers must be 1 or 2")
    if not isinstance(mission.repo, Path):
        raise ConductorError("mission repo must be a Path")
    if not mission.tasks and not mission.auto_plan:
        raise ConductorError("mission needs tasks or auto_plan=true")
    for task in mission.tasks:
        if not isinstance(task, Task):
            raise ConductorError("mission tasks must be Task instances")
        if (
            task.status != "pending"
            or task.attempts != 0
            or task.infra_strikes != 0
            or task.note
        ):
            raise ConductorError(
                f"mission task has caller-supplied runtime state: {task.id}"
            )
        Task.from_dict(
            {
                field_name: getattr(task, field_name)
                for field_name in TASK_FIELDS
            }
        )
    _validate_task_dag(mission.tasks)


def _task_spec(task: Task) -> dict[str, Any]:
    return {
        field_name: getattr(task, field_name)
        for field_name in sorted(TASK_FIELDS)
    }


def _task_spec_digest(task: Task) -> str:
    encoded = json.dumps(
        _task_spec(task),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_integrity(payload: dict[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("integrity_sha256", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _new_approval_challenge(
    task: Task,
    *,
    generation: int = 1,
) -> dict[str, Any]:
    return {
        "generation": generation,
        "task_digest": _task_spec_digest(task),
        "nonce": secrets.token_urlsafe(24),
        "used": False,
    }


# ---------------------------------------------------------------- engines

def launcher(engine: str) -> list[str]:
    """Cross-platform engine launcher argv prefix (bash on macOS, PS twin on Windows)."""
    name = {"claude": "claude-code", "opencode": "opencode", "kilo": "kilo"}[engine]
    if IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / f"engines/{name}/launch.ps1")]
    return ["bash", str(ROOT / f"engines/{name}/launch.sh")]


def shell_check(cmd: str) -> list[str]:
    """Argv for one deterministic check command."""
    if IS_WIN:
        return ["powershell", "-NoProfile", "-Command", cmd]
    return ["bash", "-lc", cmd]


def engine_cmd(engine: str, prompt: str, tier: str) -> tuple[list[str], dict]:
    """argv + extra env for one headless engine run (full autonomy: dedicated box).
    Claude Code uses stream-json WITH partial messages: token deltas stream
    continuously during generation, so the stall watchdog measures real liveness
    instead of message-boundary silence (seed brain L7 - a healthy multi-minute
    CPU generation must never look frozen)."""
    if engine == "claude":
        return (launcher("claude") + ["-p", prompt,
                 "--model", tier, "--output-format", "stream-json", "--verbose",
                 "--include-partial-messages",
                 "--dangerously-skip-permissions"], {})
    if engine == "opencode":
        return (launcher("opencode") + ["run",
                 "-m", f"oracle/{TIER_MODEL[tier]}", prompt],
                {"OPENCODE_PERMISSION": json.dumps(
                    {"edit": "allow", "bash": "allow", "webfetch": "deny"})})
    if engine == "kilo":
        # OpenCode fork; --auto = autonomous mode, bounded by the permission
        # block sync-models writes into ~/.config/kilo/kilo.jsonc (webfetch deny).
        return (launcher("kilo") + ["run", "--auto",
                 "-m", f"openai-compatible/{TIER_MODEL[tier]}", prompt], {})
    raise ValueError(f"unknown engine {engine!r} (use claude|opencode|kilo)")


def extract_result(engine: str, raw: str) -> str:
    """Final assistant text from an engine run. Claude stream-json emits one
    {"type":"result"} event at the end; OpenCode prints plain text."""
    if engine != "claude":
        return raw
    result = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "result":
            result = obj.get("result") or ""
    return result if result else raw[-4000:]


def seed_brain_index() -> str:
    """Compact ID index of the seed brain (engines/shared/SEED-BRAIN.md) â€”
    one line per principle â€” so distillation and retrospective prompts can
    cite and extend the founding memory without loading the whole file (C5)."""
    p = ROOT / "engines" / "shared" / "SEED-BRAIN.md"
    if not p.exists():
        return ""
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\*\*([OALCEVGM]\d+)\.\*\*\s*`\[(\w+)\]`\s*(.+)", line)
        if m:
            out.append(f"{m.group(1)} [{m.group(2)}]: {m.group(3)[:90]}")
    # compress rather than truncate: dropping the tail would drop the NEWEST
    # promoted principles, which are the ones the loop must see
    text = "\n".join(out)
    if len(text) > 12000:
        text = "\n".join(f"{ln[:70]}" for ln in out)
    return text


PLAN_CONTRACT = """Respond with STRICT JSON inside a single ```json fenced block: an array of
3-10 task objects, each:
{"id": "<kebab-slug>", "title": "<short>", "prompt": "<detailed, fully self-contained
instructions for an engineer with NO memory of this conversation>",
 "depends_on": ["<ids>"], "acceptance": ["<criterion>", ...],
 "checks": ["<shell command that exits 0 iff the criterion holds>", ...],
 "tier": "sonnet"|"haiku"|"opus", "timeout_minutes": <int, <=90>, "adversary": <bool>,
 "requires_approval": <bool>, "research": <bool>}
Rules: ids unique; depends_on must form a DAG over these ids; every task must be
independently verifiable; put the mechanical proof of every acceptance criterion into
checks wherever a shell command can express it; use tier "haiku" for grunt work,
"opus" only where deep reasoning is essential; adversary=true for risk-bearing tasks;
research=true for tasks entering unfamiliar code or data (a read-only researcher pass
briefs the engineer first).
Prompts are EXECUTABLE DOCUMENTS (seed brain O4): exact file paths, exact commands
with expected output, concrete acceptance semantics. "TBD", "handle edge cases",
and "similar to task N" are plan failures â€” the executing engineer has NO memory
of this conversation and cannot fill gaps you leave.
Destructive or irreversible operations (database mutations, mass deletes/merges,
schema changes, anything hard to undo) MUST be split into two tasks: a read-only
DRY-RUN task that produces a reviewable report, and an EXECUTE task that depends on
it and carries requires_approval=true. Long-running jobs (big backfills, full
walk-forwards) MUST be split into a launch task that starts a RESUMABLE,
checkpointed background job and a separate later verification task â€” never hold a
task open waiting on a long job."""


def parse_plan(text: str) -> list[Task]:
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    for block in reversed(blocks):
        try:
            raw = json.loads(block)
        except ValueError:
            continue
        if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
            continue
        try:
            if any(not isinstance(item, dict) for item in raw):
                raise ConductorError("plan tasks must be objects")
            tasks = [Task.from_dict(item) for item in raw]
            _validate_task_dag(tasks)
        except ConductorError:
            continue
        return tasks
    return []


# ---------------------------------------------------------------- conductor

class Conductor:
    def __init__(self, mission: Mission, *, resume: bool = True):
        validate_mission(mission)
        self.m = mission
        self.t0 = time.monotonic()
        self.lock = threading.RLock()       # task state + memory files/checkpoints
        self.gitlock = threading.Lock()     # worktree add / merge / branch ops
        self.biglock = threading.Lock()     # big-slot serialization (prevents swap thrash)
        self.sweeplock = threading.Lock()   # one regression sweep at a time
        self.verifylock = threading.Lock()  # exact tree from checks through merge
        self.interrupted = False
        self.current: str = "starting up"
        self.stopping = threading.Event()
        self.fatal_error: BaseException | None = None
        self.replanned = False
        for d in (MEMORY, REPORTS, LOGS, WORKTREES):
            d.mkdir(parents=True, exist_ok=True)
        self.use_git = git(self.m.repo, "rev-parse", "--git-dir").returncode == 0
        if not self.use_git:
            raise ConductorError(
                f"mission repository is not a git repository: {self.m.repo}"
            )
        base = git(self.m.repo, "rev-parse", "HEAD")
        if (
            base.returncode != 0
            or not re.fullmatch(r"[0-9a-f]{40,64}", base.stdout.strip())
        ):
            raise ConductorError("mission repository has no valid base commit")
        self.base_commit = base.stdout.strip()
        # NB: task branches live under task/<mission>/ (not under the mission branch
        # name) because git forbids a branch that is a path-prefix of another.
        self.branch = f"mission/{self.m.name}"
        self.engine_status: dict[str, dict[str, Any]] = {}
        self.mission_digest = self._mission_digest()
        runs = MEMORY / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = runs / f"{self.m.name}.checkpoint.json"
        self.checkpoint_enabled = resume
        self.run_id = uuid.uuid4().hex
        self.deadline_epoch = time.time() + float(mission.hours) * 3600
        self.resumed = False
        self.checkpoint_generation = 0
        self.pending_merge: dict[str, str] | None = None
        self.task_commits: dict[str, str] = {}
        self.owned_worktrees: set[Path] = set()
        self.review_repositories: dict[Path, Path] = {}
        self.mission_branch_owned = False
        self.owned_task_branches: set[str] = set()
        self.approval_challenges = {
            task.id: _new_approval_challenge(task)
            for task in mission.tasks
            if task.requires_approval
        }
        if (
            self.checkpoint_enabled
            and not self.checkpoint_path.exists()
            and git(
                self.m.repo,
                "rev-parse",
                "--verify",
                f"refs/heads/{self.branch}",
            ).returncode
            == 0
        ):
            raise ConductorError(
                f"existing mission branch requires explicit recovery: {self.branch}"
            )
        if self.checkpoint_enabled and self.checkpoint_path.exists():
            self._restore_checkpoint()
        self.deadline = time.monotonic() + max(
            0.0,
            self.deadline_epoch - time.time(),
        )
        self.trace_path = runs / self.run_id / "trace.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        if self.trace_path.exists():
            self._validate_trace()
        if self.checkpoint_enabled:
            self.write_checkpoint()

    def _mission_digest(self) -> str:
        payload = {
            "name": self.m.name,
            "goal": self.m.goal,
            "repo": str(self.m.repo),
            "engine": self.m.engine,
            "hours": self.m.hours,
            "report_minutes": self.m.report_minutes,
            "auto_plan": self.m.auto_plan,
            "workers": self.m.workers,
            "tasks": [_task_spec(task) for task in self.m.tasks],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _restore_checkpoint(self) -> None:
        try:
            payload = json.loads(
                self.checkpoint_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConductorError(f"checkpoint is invalid: {exc}") from exc
        try:
            integrity_valid = (
                isinstance(payload, dict)
                and isinstance(payload.get("integrity_sha256"), str)
                and payload["integrity_sha256"] == _checkpoint_integrity(payload)
            )
        except (TypeError, ValueError):
            integrity_valid = False
        if not integrity_valid:
            raise ConductorError("checkpoint integrity validation failed")
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 2
            or payload.get("mission_digest") != self.mission_digest
            or not isinstance(payload.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", payload["run_id"])
            or not isinstance(payload.get("deadline_epoch"), (int, float))
            or isinstance(payload.get("deadline_epoch"), bool)
            or not math.isfinite(float(payload["deadline_epoch"]))
            or not isinstance(payload.get("tasks"), dict)
            or not isinstance(payload.get("approval_challenges"), dict)
            or not isinstance(payload.get("task_specs"), list)
            or not isinstance(payload.get("base_commit"), str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", payload["base_commit"])
            or not isinstance(payload.get("checkpoint_generation"), int)
            or isinstance(payload.get("checkpoint_generation"), bool)
            or payload["checkpoint_generation"] < 1
        ):
            raise ConductorError(
                "checkpoint does not match the current mission specification"
            )
        try:
            restored_tasks = [
                Task.from_dict(spec)
                for spec in payload["task_specs"]
            ]
            _validate_task_dag(restored_tasks)
        except ConductorError as exc:
            raise ConductorError(
                f"checkpoint task specification is invalid: {exc}"
            ) from exc
        current_specs = {
            task.id: _task_spec(task)
            for task in self.m.tasks
        }
        restored_specs = {
            task.id: _task_spec(task)
            for task in restored_tasks
        }
        if restored_specs != current_specs:
            if not self.m.auto_plan:
                raise ConductorError(
                    "checkpoint task specification differs from mission"
                )
            for task_id, spec in current_specs.items():
                if restored_specs.get(task_id) != spec:
                    raise ConductorError(
                        "checkpoint changed a declared mission task"
                    )
            self.m.tasks = restored_tasks
        expected_ids = {task.id for task in self.m.tasks}
        if set(payload["tasks"]) != expected_ids:
            raise ConductorError("checkpoint task set is invalid")
        allowed_statuses = {
            "pending",
            "claimed",
            "running",
            "auditing",
            "done",
            "failed",
            "blocked",
            "merge-conflict",
            "regression-failed",
        }
        restored_statuses: dict[str, str] = {}
        for task in self.m.tasks:
            saved = payload["tasks"].get(task.id)
            if (
                not isinstance(saved, dict)
                or saved.get("status") not in allowed_statuses
                or not isinstance(saved.get("attempts"), int)
                or isinstance(saved.get("attempts"), bool)
                or not 0 <= saved["attempts"] <= task.max_attempts
                or not isinstance(saved.get("infra_strikes"), int)
                or isinstance(saved.get("infra_strikes"), bool)
                or not 0 <= saved["infra_strikes"] <= 3
                or not isinstance(saved.get("note"), str)
            ):
                raise ConductorError(
                    f"checkpoint task state is invalid: {task.id}"
                )
            restored_statuses[task.id] = saved["status"]
            task.status = saved["status"]
            task.attempts = saved["attempts"]
            task.infra_strikes = saved["infra_strikes"]
            task.note = saved["note"]
            if task.status in {"claimed", "running", "auditing", "regression-failed"}:
                task.status = "pending"
                task.note = "recovered after interrupted attempt"
        challenges = payload["approval_challenges"]
        if set(challenges) != {
            task.id for task in self.m.tasks if task.requires_approval
        }:
            raise ConductorError("checkpoint approval challenges are invalid")
        for task in self.m.tasks:
            if not task.requires_approval:
                continue
            challenge = challenges.get(task.id)
            if (
                not isinstance(challenge, dict)
                or set(challenge)
                != {"generation", "task_digest", "nonce", "used"}
                or not isinstance(challenge.get("generation"), int)
                or isinstance(challenge.get("generation"), bool)
                or challenge["generation"] < 1
                or challenge.get("task_digest") != _task_spec_digest(task)
                or not isinstance(challenge.get("nonce"), str)
                or len(challenge["nonce"]) < 24
                or not isinstance(challenge.get("used"), bool)
            ):
                raise ConductorError(
                    f"checkpoint approval challenge is invalid: {task.id}"
                )
        self.run_id = payload["run_id"]
        self.base_commit = payload["base_commit"]
        if git(
            self.m.repo,
            "cat-file",
            "-e",
            f"{self.base_commit}^{{commit}}",
        ).returncode != 0:
            raise ConductorError("checkpoint base commit is unavailable")
        self.deadline_epoch = float(payload["deadline_epoch"])
        self.approval_challenges = {
            task_id: dict(challenge)
            for task_id, challenge in challenges.items()
        }
        self.checkpoint_generation = payload["checkpoint_generation"]
        self.replanned = bool(payload.get("replanned", False))
        mission_branch_owned = payload.get("mission_branch_owned")
        owned_task_branches = payload.get("owned_task_branches")
        if (
            not isinstance(mission_branch_owned, bool)
            or not isinstance(owned_task_branches, list)
            or any(
                not isinstance(branch, str)
                or not branch.startswith(f"task/{self.m.name}/")
                for branch in owned_task_branches
            )
        ):
            raise ConductorError("checkpoint branch ownership is invalid")
        self.mission_branch_owned = mission_branch_owned
        self.owned_task_branches = set(owned_task_branches)
        if self.mission_branch_owned and git(
            self.m.repo,
            "rev-parse",
            "--verify",
            f"refs/heads/{self.branch}",
        ).returncode != 0:
            raise ConductorError("checkpoint mission branch is missing")
        if self.mission_branch_owned and git(
            self.m.repo,
            "merge-base",
            "--is-ancestor",
            self.base_commit,
            self.branch,
        ).returncode != 0:
            raise ConductorError("checkpoint mission branch does not contain its base")
        expected_head = payload.get("mission_head")
        pending_merge = payload.get("pending_merge")
        if expected_head is not None and (
            not isinstance(expected_head, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", expected_head)
        ):
            raise ConductorError("checkpoint mission head is invalid")
        if pending_merge is not None and (
            not isinstance(pending_merge, dict)
            or set(pending_merge) != {"task_id", "expected_head", "task_tip"}
            or pending_merge.get("task_id") not in expected_ids
            or not re.fullmatch(
                r"[0-9a-f]{40,64}",
                str(pending_merge.get("expected_head", "")),
            )
            or not re.fullmatch(
                r"[0-9a-f]{40,64}",
                str(pending_merge.get("task_tip", "")),
            )
            or pending_merge.get("expected_head") != expected_head
        ):
            raise ConductorError("checkpoint pending merge is invalid")
        recovered_merge_task: str | None = None
        if self.mission_branch_owned:
            current_head_result = git(self.m.repo, "rev-parse", self.branch)
            if (
                current_head_result.returncode != 0
                or not re.fullmatch(
                    r"[0-9a-f]{40,64}",
                    current_head_result.stdout.strip(),
                )
            ):
                raise ConductorError("checkpoint mission branch is unreadable")
            current_head = current_head_result.stdout.strip()
            if pending_merge is None:
                if current_head != expected_head:
                    raise ConductorError(
                        "checkpoint mission branch moved outside durable state"
                    )
            else:
                task_id = str(pending_merge["task_id"])
                task_tip = str(pending_merge["task_tip"])
                expected_old = str(pending_merge["expected_head"])
                task = next(item for item in self.m.tasks if item.id == task_id)
                if (
                    task.attempts < 1
                    or restored_statuses.get(task_id)
                    not in {"claimed", "running", "auditing"}
                    or git(
                        self.m.repo,
                        "merge-base",
                        "--is-ancestor",
                        expected_old,
                        task_tip,
                    ).returncode
                    != 0
                ):
                    raise ConductorError("checkpoint pending merge evidence is invalid")
                if current_head == expected_old:
                    applied = git(
                        self.m.repo,
                        "update-ref",
                        f"refs/heads/{self.branch}",
                        task_tip,
                        expected_old,
                    )
                    if applied.returncode != 0:
                        raise ConductorError(
                            "checkpoint pending merge could not be reconciled"
                        )
                    current_head = task_tip
                elif current_head != task_tip:
                    raise ConductorError(
                        "checkpoint mission branch conflicts with pending merge"
                    )
                task.status = "done"
                task.note = ""
                recovered_merge_task = task_id
        elif expected_head is not None or pending_merge is not None:
            raise ConductorError("checkpoint claims a head for an unowned mission branch")
        self.pending_merge = None
        commits = payload.get("task_commits")
        if not isinstance(commits, dict) or set(commits) != expected_ids:
            raise ConductorError("checkpoint task commit map is invalid")
        if recovered_merge_task is not None:
            commits = dict(commits)
            commits[recovered_merge_task] = pending_merge["task_tip"]
        for task in self.m.tasks:
            commit = commits.get(task.id)
            if task.status == "done":
                if (
                    not isinstance(commit, str)
                    or not re.fullmatch(r"[0-9a-f]{40,64}", commit)
                    or commit == self.base_commit
                    or task.attempts < 1
                    or git(
                        self.m.repo,
                        "merge-base",
                        "--is-ancestor",
                        commit,
                        self.branch,
                    ).returncode
                    != 0
                ):
                    raise ConductorError(
                        f"checkpoint completed task is not on mission branch: {task.id}"
                    )
                self.task_commits[task.id] = commit
            elif commit is not None:
                raise ConductorError(
                    f"checkpoint unfinished task has a commit claim: {task.id}"
                )
        done_ids = {task.id for task in self.m.tasks if task.status == "done"}
        for task in self.m.tasks:
            if task.status == "done" and any(
                dependency not in done_ids
                for dependency in task.depends_on
            ):
                raise ConductorError(
                    f"checkpoint completed task has incomplete dependencies: {task.id}"
                )
        self.resumed = True

    def _validate_trace(self) -> None:
        try:
            lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConductorError(f"trace is unreadable: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConductorError(
                    f"trace line {number} is malformed"
                ) from exc
            if (
                not isinstance(record, dict)
                or record.get("schema_version") != 1
                or record.get("run_id") != self.run_id
                or not isinstance(record.get("kind"), str)
            ):
                raise ConductorError(f"trace line {number} is invalid")
            task_id = record.get("task")
            attempt = record.get("attempt")
            invocation_id = record.get("invocation_id")
            if invocation_id is not None and (
                not isinstance(invocation_id, str)
                or not re.fullmatch(
                    rf"{self.run_id}:[0-9a-f]{{32}}",
                    invocation_id,
                )
            ):
                raise ConductorError(
                    f"trace line {number} has an invalid invocation id"
                )
            if task_id is not None and (
                not isinstance(task_id, str)
                or record.get("task_id") != f"{self.run_id}:{task_id}"
            ):
                raise ConductorError(
                    f"trace line {number} has an invalid task id"
                )
            if attempt is not None and (
                not isinstance(attempt, int)
                or isinstance(attempt, bool)
                or not isinstance(task_id, str)
                or record.get("attempt_id")
                != (
                    invocation_id
                    if invocation_id is not None
                    else f"{self.run_id}:{task_id}:{attempt}"
                )
            ):
                raise ConductorError(
                    f"trace line {number} has an invalid attempt id"
                )

    def _current_mission_head(self) -> str | None:
        if not self.mission_branch_owned:
            return None
        result = git(self.m.repo, "rev-parse", self.branch)
        if (
            result.returncode != 0
            or not re.fullmatch(r"[0-9a-f]{40,64}", result.stdout.strip())
        ):
            raise ConductorError("owned mission branch has no readable head")
        return result.stdout.strip()

    def _checkpoint_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "checkpoint_generation": self.checkpoint_generation,
            "run_id": self.run_id,
            "base_commit": self.base_commit,
            "mission_digest": self.mission_digest,
            "deadline_epoch": self.deadline_epoch,
            "replanned": self.replanned,
            "mission_branch_owned": self.mission_branch_owned,
            "mission_head": self._current_mission_head(),
            "pending_merge": self.pending_merge,
            "owned_task_branches": sorted(self.owned_task_branches),
            "approval_challenges": self.approval_challenges,
            "task_specs": [_task_spec(task) for task in self.m.tasks],
            "task_commits": {
                task.id: self.task_commits.get(task.id)
                for task in self.m.tasks
            },
            "tasks": {
                task.id: {
                    "status": task.status,
                    "attempts": task.attempts,
                    "infra_strikes": task.infra_strikes,
                    "note": task.note,
                }
                for task in self.m.tasks
            },
        }

    def write_checkpoint(self) -> None:
        if not self.checkpoint_enabled:
            return
        with self.lock:
            self.checkpoint_generation += 1
            payload = self._checkpoint_payload()
            payload["integrity_sha256"] = _checkpoint_integrity(payload)
            atomic_write_text(
                self.checkpoint_path,
                json.dumps(
                    payload,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n",
            )

    def task_branch(self, t: Task) -> str:
        return f"task/{self.m.name}/{t.id}"

    # ---- memory -------------------------------------------------------------
    def ledger(self, event: str, detail: str = "") -> None:
        line = f"- **{now()}** [{self.m.name}] {event}" + (f" â€” {detail}" if detail else "")
        path = MEMORY / "LEDGER.md"
        with self.lock:
            if not path.exists():
                path.write_text("# SentiVue Oracle â€” Ledger (append-only)\n\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        print(line, flush=True)

    def proc(self, kind: str, **kw) -> None:
        """Process telemetry: one JSON line per process event in memory/PROCESS.jsonl.
        This is the dataset the retrospective meta-analyzes â€” the loop documents its
        own working, auditing, and looping behavior as structured data."""
        rec: dict[str, Any] = {
            "schema_version": 1,
            "ts": now(),
            "mission": self.m.name,
            "run_id": self.run_id,
            "kind": kind,
            **kw,
        }
        task_id = rec.get("task")
        attempt = rec.get("attempt")
        if isinstance(task_id, str):
            rec["task_id"] = f"{self.run_id}:{task_id}"
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                invocation_id = rec.get("invocation_id")
                rec["charged_attempt"] = attempt
                rec["attempt_id"] = (
                    invocation_id
                    if isinstance(invocation_id, str)
                    else f"{self.run_id}:{task_id}:{attempt}"
                )
        with self.lock:
            append_jsonl_durable(MEMORY / "PROCESS.jsonl", rec)
            append_jsonl_durable(self.trace_path, rec)

    def log_failure(self, t: Task, kind: str, detail: str) -> None:
        """Failure memory: future runs grep this before attempting anything â€”
        the cheapest way to stop a 24 h mission from re-running dead ends."""
        p = MEMORY / "FAILURES.md"
        with self.lock:
            if not p.exists():
                p.write_text("# Failure memory â€” what did not work and why. "
                             "Search this before any risky attempt.\n\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as f:
                f.write(f"## {now()} Â· {self.m.name}/{t.id} Â· attempt {t.attempts} Â· {kind}\n"
                        f"{detail.strip()[:600]}\n\n")

    def write_state(self) -> None:
        rows = "\n".join(
            f"| {t.id} | {t.status} | {t.attempts}/{t.max_attempts} | {t.tier} | {t.title} |"
            for t in self.m.tasks)
        left = max(0.0, (self.deadline - time.monotonic()) / 3600)
        state = (
            f"# STATE â€” mission `{self.m.name}` ({self.m.engine} engine)\n\n"
            f"Updated: {now()} Â· Time left: {left:.1f} h\nGoal: {self.m.goal}\n\n"
            f"Now: {self.current}\n\n"
            f"| task | status | attempts | tier | title |\n|---|---|---|---|---|\n{rows}\n"
        )
        with self.lock:
            done_ids = {task.id for task in self.m.tasks if task.status == "done"}
            pending = []
            for task in self.m.tasks:
                if (
                    task.status != "pending"
                    or not task.requires_approval
                    or task.id not in self.approval_challenges
                    or not all(dep in done_ids for dep in task.depends_on)
                    or self.approved(task)
                    or self.denied(task)
                ):
                    continue
                challenge = self._challenge_for(task)
                pending.append(
                    {
                        "run_id": self.run_id,
                        "task_id": task.id,
                        "generation": challenge["generation"],
                        "task_digest": challenge["task_digest"],
                        "nonce": challenge["nonce"],
                        "command": self.approval_command(task),
                        "deny_command": self.denial_command(task),
                    }
                )
            atomic_write_text(MEMORY / "STATE.md", state)
            atomic_write_text(
                MEMORY / "PENDING-APPROVALS.json",
                json.dumps(
                    {
                        "schema_version": 2,
                        "pending": pending,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            self.write_checkpoint()

    # ---- self-healing -------------------------------------------------------
    def ensure_tools(self) -> None:
        """Self-provision the toolbelt before work starts. Doctrine: a missing
        tool is a task, not a blocker â€” install it (or queue a NET-REQUEST on the
        air-gapped node) instead of failing the mission on it."""
        script = ROOT / ("bootstrap/ensure-tools.ps1" if IS_WIN else "bootstrap/ensure-tools.sh")
        if not script.exists():
            return
        argv = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
                if IS_WIN else ["bash", str(script)])
        r = sh(argv, timeout=900)
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(no output)"
        self.ledger("TOOLBELT", tail[:300])

    def ensure_serving(self) -> None:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(SWAP_HEALTH, timeout=5):
                    return
            except Exception:
                self.ledger("SELF-HEAL", f"llama-swap unhealthy â€” restart attempt {attempt + 1}")
                if IS_WIN:
                    sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "serving/serve-windows.ps1"), "start"], timeout=180)
                else:
                    sh(["bash", str(ROOT / "serving/service.sh"), "restart"], timeout=120)
                time.sleep(30)
        self.ledger("SELF-HEAL FAILED", "llama-swap did not recover; continuing (runs will fail fast)")

    # ---- worktrees ----------------------------------------------------------
    def _ensure_mission_branch(self) -> None:
        if git(
            self.m.repo,
            "rev-parse",
            "--verify",
            f"refs/heads/{self.branch}",
        ).returncode == 0:
            if not self.resumed and not self.mission_branch_owned:
                raise ConductorError(
                    f"existing mission branch requires explicit recovery: "
                    f"{self.branch}"
                )
            return
        result = git(self.m.repo, "branch", self.branch, self.base_commit)
        if result.returncode != 0:
            raise ConductorError(
                f"could not create mission branch {self.branch}: "
                f"{result.stdout[-300:]}"
            )
        self.mission_branch_owned = True
        self.write_checkpoint()

    def _verify_existing_worktree(self, wt: Path, branch: str) -> None:
        top = git(wt, "rev-parse", "--show-toplevel")
        current = git(wt, "branch", "--show-current")
        if (
            top.returncode != 0
            or Path(top.stdout.strip()).resolve() != wt.resolve()
            or current.returncode != 0
            or current.stdout.strip() != branch
        ):
            raise ConductorError(
                f"existing worktree is not owned by {branch}: {wt}"
            )

    def _make_review_worktree(
        self,
        source: Path,
        label: str,
    ) -> tuple[Path, str]:
        """Create an isolated detached artifact for a read-only model review."""

        expected = git(source, "rev-parse", "HEAD")
        if (
            expected.returncode != 0
            or not re.fullmatch(r"[0-9a-f]{40,64}", expected.stdout.strip())
        ):
            raise ConductorError("could not bind review to a source commit")
        repository_result = git(source, "rev-parse", "--show-toplevel")
        if repository_result.returncode != 0:
            raise ConductorError("could not identify review source repository")
        repository = Path(repository_result.stdout.strip()).resolve()
        review = WORKTREES / (
            f"{self.m.name}-{label}-{self.run_id[:8]}-"
            f"{secrets.token_hex(4)}"
        )
        with self.gitlock:
            result = git(
                repository,
                "worktree",
                "add",
                "--detach",
                str(review),
                expected.stdout.strip(),
            )
        if result.returncode != 0:
            raise ConductorError(
                f"could not create isolated review worktree ({label}): "
                f"{result.stdout[-300:]}"
            )
        self.owned_worktrees.add(review)
        self.review_repositories[review] = repository
        return review, expected.stdout.strip()

    def _finish_review_worktree(
        self,
        review: Path,
        expected_head: str,
    ) -> bool:
        """Remove an unchanged review worktree; preserve mutation as evidence."""

        observed = git(review, "rev-parse", "HEAD")
        status = git(review, "status", "--porcelain", "--untracked-files=all")
        unchanged = (
            observed.returncode == 0
            and observed.stdout.strip() == expected_head
            and status.returncode == 0
            and not status.stdout.strip()
        )
        if not unchanged:
            self.ledger(
                "REVIEW MUTATION",
                f"isolated reviewer modified {review}; artifact preserved",
            )
            return False
        with self.gitlock:
            repository = self.review_repositories.get(review)
            if repository is None:
                raise ConductorError(
                    f"review worktree repository ownership is missing: {review}"
                )
            removed = git(
                repository,
                "worktree",
                "remove",
                str(review),
            )
        if removed.returncode != 0:
            self.ledger(
                "CLEANUP REFUSED",
                f"review worktree removal failed: {review}",
            )
            return False
        self.owned_worktrees.discard(review)
        self.review_repositories.pop(review, None)
        return True

    def run_readonly_engine(
        self,
        prompt: str,
        tier: str,
        source: Path,
        timeout_min: int,
        tag: str,
        *,
        stall_min: int | None = None,
    ) -> str:
        """Run a read-only role in an isolated worktree and reject mutations."""

        safe_label = re.sub(r"[^a-z0-9-]+", "-", tag.lower()).strip("-")
        review, expected_head = self._make_review_worktree(
            source,
            safe_label or "review",
        )
        try:
            output = self.run_engine(
                prompt,
                tier,
                review,
                timeout_min,
                tag,
                stall_min=stall_min,
            )
        finally:
            clean = self._finish_review_worktree(review, expected_head)
        if not clean:
            raise ConductorError(
                f"read-only engine role mutated isolated artifact: {tag}"
            )
        return output

    def make_worktree(self, t: Task) -> Path:
        """One worktree per task, reused across retry attempts so the developer
        iterates on prior work instead of starting over."""
        with self.gitlock:
            self._ensure_mission_branch()
            wt = WORKTREES / f"{self.m.name}-{t.id}"
            task_branch = self.task_branch(t)
            if wt.exists():
                if task_branch not in self.owned_task_branches:
                    raise ConductorError(
                        f"unpublished prior worktree requires explicit recovery: {wt}"
                    )
                self._verify_existing_worktree(wt, task_branch)
                self.owned_worktrees.add(wt)
                return wt
            git(self.m.repo, "worktree", "prune")
            branch_exists = git(
                self.m.repo,
                "rev-parse",
                "--verify",
                f"refs/heads/{task_branch}",
            ).returncode == 0
            if branch_exists and task_branch not in self.owned_task_branches:
                raise ConductorError(
                    f"unpublished prior task branch requires explicit recovery: "
                    f"{task_branch}"
                )
            args = (
                ["worktree", "add", str(wt), task_branch]
                if branch_exists
                else [
                    "worktree",
                    "add",
                    "-b",
                    task_branch,
                    str(wt),
                    self.branch,
                ]
            )
            r = git(self.m.repo, *args)
            if r.returncode != 0:
                self.ledger("WORKTREE ERROR", r.stdout[-400:])
                raise ConductorError(
                    f"worktree creation failed for {t.id}: {r.stdout[-300:]}"
                )
            self.owned_worktrees.add(wt)
            self.owned_task_branches.add(task_branch)
            self.write_checkpoint()
            return wt

    def prepare_verification(
        self,
        t: Task,
        wt: Path,
        branch_name: str | None = None,
    ) -> bool:
        """Commit the candidate and rebase it before any final gate runs."""

        with self.gitlock:
            add = git(wt, "add", "-A")
            if add.returncode != 0:
                t.note = f"could not stage candidate: {add.stdout[-200:]}"
                return False
            dirty = git(wt, "status", "--porcelain")
            if dirty.returncode != 0:
                t.note = "could not inspect candidate status"
                return False
            if dirty.stdout.strip():
                commit = git(
                    wt,
                    "commit",
                    "-m",
                    f"wip({t.id}): candidate before final verification",
                )
                if commit.returncode != 0:
                    t.note = f"could not commit candidate: {commit.stdout[-200:]}"
                    return False
            task_branch = branch_name or self.task_branch(t)
            if git(
                self.m.repo,
                "merge-base",
                "--is-ancestor",
                self.branch,
                task_branch,
            ).returncode != 0:
                result = git(wt, "rebase", self.branch)
                if result.returncode != 0:
                    git(wt, "rebase", "--abort")
                    t.note = f"rebase onto {self.branch} failed"
                    return False
            return True

    def merge_task(self, t: Task, wt: Path, branch_name: str | None = None) -> bool:
        """Atomically advance the mission branch to the exact audited task tip.

        Verification must call ``prepare_verification`` before checks/audit.
        This method never changes the candidate tree after audit and uses a
        compare-and-swap ref update plus a durable merge intent."""
        with self.gitlock:
            add = git(wt, "add", "-A")
            if add.returncode != 0:
                t.status = "merge-conflict"
                t.note = f"could not stage audited work: {add.stdout[-200:]}"
                return False
            dirty = git(wt, "status", "--porcelain")
            if dirty.returncode != 0:
                t.status = "merge-conflict"
                t.note = "could not inspect audited worktree status"
                return False
            if dirty.stdout.strip():
                commit = git(
                    wt,
                    "commit",
                    "-m",
                    f"task({t.id}): {t.title} [audit: pass]",
                )
                if commit.returncode != 0:
                    t.status = "merge-conflict"
                    t.note = f"could not commit audited work: {commit.stdout[-200:]}"
                    return False
            task_branch = branch_name or self.task_branch(t)
            expected = git(self.m.repo, "rev-parse", self.branch)
            tip = git(self.m.repo, "rev-parse", task_branch)
            if (
                expected.returncode != 0
                or tip.returncode != 0
                or not re.fullmatch(r"[0-9a-f]{40,64}", expected.stdout.strip())
                or not re.fullmatch(r"[0-9a-f]{40,64}", tip.stdout.strip())
                or git(
                    self.m.repo,
                    "merge-base",
                    "--is-ancestor",
                    expected.stdout.strip(),
                    tip.stdout.strip(),
                ).returncode
                != 0
            ):
                t.status = "merge-conflict"
                t.note = "candidate changed or mission advanced after final verification"
                return False
            regressions_ok, regression_detail = self.run_regression_gates(t, wt)
            if not regressions_ok:
                t.status = "regression-failed"
                t.note = regression_detail[:300]
                self.ledger(
                    f"REGRESSION BLOCK {t.id}",
                    f"mission branch not advanced; {t.note}",
                )
                return False
            with self.lock:
                self.pending_merge = {
                    "task_id": t.id,
                    "expected_head": expected.stdout.strip(),
                    "task_tip": tip.stdout.strip(),
                }
                self.write_checkpoint()
            updated = git(
                self.m.repo,
                "update-ref",
                f"refs/heads/{self.branch}",
                tip.stdout.strip(),
                expected.stdout.strip(),
            )
            if updated.returncode != 0:
                raise ConductorError(
                    f"mission branch compare-and-swap failed for {t.id}; "
                    "durable merge intent was preserved"
                )
            with self.lock:
                self.task_commits[t.id] = tip.stdout.strip()
                t.status = "done"
                t.note = ""
                self.pending_merge = None
                self.write_checkpoint()
        self.vault_backup([self.branch])
        self.drop_worktree(wt, task_branch)
        return True

    def make_candidate_worktree(self, t: Task, k: int) -> tuple[Path | None, str]:
        """Independent worktree+branch for tournament candidate k (fresh from the
        mission tip - candidates never see each other's work)."""
        with self.gitlock:
            self._ensure_mission_branch()
            branch = f"{self.task_branch(t)}-cand{k}-{self.run_id[:8]}"
            wt = WORKTREES / f"{self.m.name}-{t.id}-cand{k}-{self.run_id[:8]}"
            if wt.exists():
                if branch not in self.owned_task_branches:
                    raise ConductorError(
                        f"candidate branch is not checkpoint-owned: {branch}"
                    )
                self._verify_existing_worktree(wt, branch)
                self.owned_worktrees.add(wt)
                return wt, branch
            git(self.m.repo, "worktree", "prune")
            r = git(self.m.repo, "worktree", "add", "-b", branch, str(wt), self.branch)
            if r.returncode != 0:
                self.ledger("WORKTREE ERROR", f"cand{k}: {r.stdout[-300:]}")
                raise ConductorError(
                    f"candidate worktree creation failed: {r.stdout[-300:]}"
                )
            self.owned_worktrees.add(wt)
            self.owned_task_branches.add(branch)
            self.write_checkpoint()
            return wt, branch

    def drop_worktree(self, wt: Path | None, branch: str) -> bool:
        """Delete only clean work already merged and present on a remote."""

        if wt is None:
            return False
        with self.gitlock:
            if not wt.exists() or not branch:
                return False
            self._verify_existing_worktree(wt, branch)
            status = git(wt, "status", "--porcelain")
            if status.returncode != 0 or status.stdout.strip():
                self.ledger(
                    "CLEANUP REFUSED",
                    f"dirty worktree preserved: {wt}",
                )
                return False
            tip = git(self.m.repo, "rev-parse", branch)
            if (
                tip.returncode != 0
                or git(
                    self.m.repo,
                    "merge-base",
                    "--is-ancestor",
                    branch,
                    self.branch,
                ).returncode
                != 0
            ):
                self.ledger(
                    "CLEANUP REFUSED",
                    f"unmerged branch preserved: {branch}",
                )
                return False
            published = git(
                self.m.repo,
                "for-each-ref",
                "--format=%(refname)",
                "--contains",
                tip.stdout.strip(),
                "refs/remotes",
            )
            if published.returncode != 0 or not published.stdout.strip():
                self.ledger(
                    "CLEANUP REFUSED",
                    f"unpublished branch/worktree preserved: {branch}",
                )
                return False
            removed = git(self.m.repo, "worktree", "remove", str(wt))
            if removed.returncode != 0:
                self.ledger(
                    "CLEANUP REFUSED",
                    f"worktree removal failed: {removed.stdout[-200:]}",
                )
                return False
            deleted = git(self.m.repo, "branch", "-d", branch)
            if deleted.returncode != 0:
                self.ledger(
                    "CLEANUP REFUSED",
                    f"safe branch deletion failed: {branch}",
                )
                return False
            self.owned_worktrees.discard(wt)
            self.owned_task_branches.discard(branch)
            self.write_checkpoint()
            return True

    def vault_backup(self, refs: list[str] | None = None) -> None:
        """Push work to the local 'vault' remote (bare repo on this machine â€”
        the air-gapped stand-in for a hosted origin). Silent no-op when the
        vault remote isn't configured; failures warn but never block work."""
        if not self.use_git:
            return
        if git(self.m.repo, "remote", "get-url", "vault").returncode != 0:
            return
        args = ["push", "--quiet", "vault"] + (refs if refs else ["--all"])
        r = git(self.m.repo, *args, timeout=120)
        if r.returncode != 0:
            self.ledger("VAULT WARN", f"backup push failed: {r.stdout.strip()[-200:]}")

    # ---- engine runs ---------------------------------------------------------
    def run_engine(self, prompt: str, tier: str, cwd: Path, timeout_min: int,
                   tag: str, stall_min: int | None = None) -> str:
        self.ensure_serving()
        argv, env = engine_cmd(self.m.engine, prompt, tier)
        # Only one big-slot (sonnet/opus) request at a time: concurrent big requests
        # from the parallel worker would thrash the model hot-swap. Fast-lane requests
        # (haiku model, always resident, --parallel 2) run without the lock.
        need_big = TIER_MODEL[tier] != TIER_MODEL["haiku"]
        gate = self.biglock if need_big else contextlib.nullcontext()
        with gate:
            r = sh(argv, cwd=cwd, timeout=timeout_min * 60, env=env,
                   stall_timeout=stall_min * 60 if stall_min else None)
        invocation_uuid = uuid.uuid4().hex
        invocation_id = f"{self.run_id}:{invocation_uuid}"
        log_path = LOGS / self.run_id / f"{invocation_uuid}.log"
        atomic_write_text(log_path, r.stdout)
        text = extract_result(self.m.engine, r.stdout)
        if "[WATCHDOG]" in r.stdout:
            text += "\n[WATCHDOG] killed"
        self.engine_status[tag] = {
            "returncode": r.returncode,
            "watchdog": "[WATCHDOG]" in r.stdout,
            "output_nonempty": bool(text.strip()),
            "invocation_id": invocation_id,
            "log_path": str(log_path),
        }
        task_id = next(
            (
                task.id
                for task in self.m.tasks
                if tag == task.id or tag.startswith(f"{task.id}-")
            ),
            None,
        )
        attempt_match = re.search(r"-dev(\d+)$", tag)
        attempt_number = (
            int(attempt_match.group(1))
            if attempt_match
            else None
        )
        if task_id is not None and attempt_number is None:
            task = next(item for item in self.m.tasks if item.id == task_id)
            attempt_number = task.attempts if task.attempts > 0 else None
        self.proc(
            "engine",
            tag=tag,
            tier=tier,
            returncode=r.returncode,
            watchdog="[WATCHDOG]" in r.stdout,
            task=task_id,
            attempt=attempt_number,
            invocation_id=invocation_id,
            log_path=str(log_path),
        )
        return text

    def engine_failure(self, tag: str, output: str) -> str | None:
        """Classify engine/runtime failures independently from task quality."""

        status = self.engine_status.get(tag)
        if status and status.get("returncode") != 0:
            return f"engine exited {status['returncode']}"
        if status and status.get("watchdog"):
            return "engine process was terminated by watchdog"
        stripped = output.strip()
        if not stripped:
            return "engine produced empty output"
        if (
            re.match(r"API Error:", stripped)
            or "exceeds the available context size" in stripped
            or (
                len(stripped) < 500
                and re.search(
                    r"\b(ECONNREFUSED|fetch failed|connection refused)\b",
                    stripped,
                    re.I,
                )
            )
        ):
            return stripped[:400]
        return None

    # ---- planning ------------------------------------------------------------
    def plan_mission(self) -> bool:
        self.current = "planning: decomposing the mission goal"
        self.ledger("PLANNING", "auto_plan: decomposing goal into tasks")
        tier = "opus" if TIER_MODEL["opus"] != TIER_MODEL["sonnet"] else "sonnet"
        prompt = (
            f"You are the mission PLANNER.\nMISSION GOAL: {self.m.goal}\n"
            f"Repository: {self.m.repo}\n\n"
            "Investigate the repository READ-ONLY first (git log, key files, tests, docs) "
            "so the plan reflects reality, then decompose the goal into an executable task "
            "DAG. Each task is later executed by a fresh engineer in an isolated worktree "
            "and verified by an independent auditor against its acceptance criteria and "
            f"checks.\n\n{PLAN_CONTRACT}"
        )
        out = self.run_readonly_engine(
            prompt,
            tier,
            self.m.repo,
            30,
            "plan",
            stall_min=12,
        )
        tasks = parse_plan(out)
        if not tasks:
            self.ledger("PLANNING FAILED", "no valid JSON plan produced")
            return False
        background = [t for t in self.m.tasks if t.background]
        retained_ids = {task.id for task in background}
        old_challenges = dict(self.approval_challenges)
        self.m.tasks = tasks + background
        _validate_task_dag(self.m.tasks)
        self.approval_challenges = {
            task.id: (
                dict(old_challenges[task.id])
                if (
                    task.id in retained_ids
                    and task.id in old_challenges
                    and old_challenges[task.id].get("task_digest")
                    == _task_spec_digest(task)
                )
                else _new_approval_challenge(task)
            )
            for task in self.m.tasks
            if task.requires_approval
        }
        self.task_commits = {
            task_id: commit
            for task_id, commit in self.task_commits.items()
            if task_id in {task.id for task in self.m.tasks}
        }
        self.write_state()
        plan_md = "\n".join(f"- **{t.id}** ({t.tier}, deps: {t.depends_on or 'â€”'}): {t.title}"
                            for t in tasks)
        (MEMORY / "MISSION-PLAN.md").write_text(
            f"# Mission plan â€” {self.m.name}\n\n{now()}\nGoal: {self.m.goal}\n\n{plan_md}\n",
            encoding="utf-8")
        self.ledger("PLANNED", f"{len(tasks)} tasks: " + ", ".join(t.id for t in tasks))
        return True

    def replan(self) -> bool:
        """One bounded recovery replan when the task DAG stalls on failures."""
        self.current = "replanning after stall"
        table = "\n".join(f"- {t.id}: {t.status} ({t.note[:120]})" for t in self.m.tasks)
        failures = ""
        fpath = MEMORY / "FAILURES.md"
        if fpath.exists():
            failures = fpath.read_text(encoding="utf-8", errors="replace")[-3000:]
        tier = "opus" if TIER_MODEL["opus"] != TIER_MODEL["sonnet"] else "sonnet"
        prompt = (
            f"You are the mission PLANNER. The plan has STALLED.\n"
            f"MISSION GOAL: {self.m.goal}\nRepository: {self.m.repo}\n\n"
            f"TASK STATE:\n{table}\n\nFAILURE MEMORY (most recent):\n{failures}\n\n"
            "Produce REPLACEMENT tasks that route around the failures (different approach, "
            "smaller steps, or explicit diagnostics first). Completed tasks are kept; every "
            "failed/blocked/pending task is discarded in favor of your new plan. Do not "
            f"re-propose an approach the failure memory shows to be dead.\n\n{PLAN_CONTRACT}"
        )
        out = self.run_readonly_engine(
            prompt,
            tier,
            self.m.repo,
            30,
            "replan",
            stall_min=12,
        )
        new = parse_plan(out)
        if not new:
            self.ledger("REPLAN FAILED", "no valid JSON plan produced")
            return False
        keep = [t for t in self.m.tasks if t.status == "done" or t.background]
        valid_ids = {t.id for t in keep} | {t.id for t in new}
        if len(valid_ids) != len(keep) + len(new):
            self.ledger("REPLAN FAILED", "replacement task id collides with retained task")
            return False
        with self.lock:
            self.m.tasks = keep + new
            _validate_task_dag(self.m.tasks)
            old_challenges = dict(self.approval_challenges)
            retained_ids = {task.id for task in keep}
            self.approval_challenges = {
                task.id: (
                    dict(old_challenges[task.id])
                    if (
                        task.id in retained_ids
                        and task.id in old_challenges
                        and old_challenges[task.id].get("task_digest")
                        == _task_spec_digest(task)
                    )
                    else _new_approval_challenge(task)
                )
                for task in self.m.tasks
                if task.requires_approval
            }
            self.task_commits = {
                task.id: self.task_commits[task.id]
                for task in keep
                if task.id in self.task_commits
            }
            self.write_checkpoint()
        self.ledger("REPLANNED", f"{len(new)} recovery tasks: " + ", ".join(t.id for t in new))
        return True

    # ---- research pass ---------------------------------------------------------
    def research_pass(self, t: Task, wt: Path) -> str:
        """Read-only reconnaissance on the fast lane before the first developer
        attempt: code archaeology, data profiling, prior art in the ledger. The
        brief is persisted to the worktree and injected into the developer prompt."""
        prompt = (
            f"You are the RESEARCHER for task [{t.id}] '{t.title}'. STRICTLY READ-ONLY: "
            "modify nothing; your only output is a findings brief for the engineer who "
            "implements this task next.\n\n"
            f"TASK BRIEF:\n{t.prompt}\n\n"
            "Method: survey the code that this task will touch (grep/glob, then read the "
            "load-bearing files completely); profile any relevant data; check "
            "memory/LEDGER.md and memory/FAILURES.md for prior art and dead ends.\n"
            "Report under 60 lines: FINDINGS (numbered, each with file:line or query "
            "evidence), RISKS (what could invalidate the plan), RECOMMENDATION (the "
            "single approach you would take, and why)."
        )
        out = self.run_readonly_engine(
            prompt,
            "haiku",
            wt,
            12,
            f"{t.id}-research",
            stall_min=8,
        ).strip()
        if out:
            atomic_write_text(
                wt / "RESEARCH.md",
                f"# Researcher brief â€” {t.id}\n\n{out}\n",
            )
            self.ledger(f"RESEARCH {t.id}", f"brief written ({len(out.splitlines())} lines)")
            self.proc("research", task=t.id, lines=len(out.splitlines()))
        return out

    # ---- prompts ---------------------------------------------------------------
    def developer_prompt(self, t: Task, feedback: str, brief: str = "") -> str:
        acc = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(t.acceptance)) or "  (none listed)"
        gates = ""
        if t.checks:
            gates = ("\n\nMECHANICAL GATES â€” the conductor runs these itself after you finish; "
                     "every one must exit 0:\n" +
                     "\n".join(f"  $ {c}" for c in t.checks))
        fb = ""
        if feedback:
            fb = (f"\n\nATTEMPT {t.attempts} â€” THE PREVIOUS ATTEMPT FAILED. Full details are in "
                  "FEEDBACK.md in this directory; the summary:\n"
                  f"{feedback[:1200]}\n"
                  "Do NOT re-run the failed approach harder. First write the DIAGNOSIS block in "
                  "TASKPLAN.md (root cause, not symptom â€” 5 lines), then execute a changed plan. "
                  "Repeating a logged failure wastes the mission's budget.")
        rb = ""
        if brief:
            rb = ("\n\nRESEARCHER BRIEF (read-only recon done for you â€” full text in "
                  f"RESEARCH.md here):\n{brief[:2500]}")
        return (
            f"MISSION: {self.m.goal}\nTASK [{t.id}]: {t.title}\n\n{t.prompt}{rb}\n\n"
            f"ACCEPTANCE CRITERIA (audited independently â€” all must demonstrably hold):\n"
            f"{acc}{gates}{fb}\n\n"
            "You operate under the Long-Horizon Autonomy Protocol (in your loaded instructions). "
            "Non-negotiables for this run:\n"
            f"1. START RITUAL before any edit: read {MEMORY / 'STATE.md'}, the tail of "
            f"{MEMORY / 'LEDGER.md'}, {MEMORY / 'LESSONS.md'} (hard-won knowledge â€” do not "
            f"relearn it), and {MEMORY / 'FAILURES.md'} (search '{t.id}'); "
            "run `git log --oneline -10` and `git status` here; read TASKPLAN.md if present.\n"
            "2. Write TASKPLAN.md (GOAL / 3-7 STEPS each with a CHECK / NOT-DOING) before "
            "touching code; keep it updated â€” it is your anchor against drift.\n"
            "3. Work the ratchet: one step, run its CHECK, commit "
            "(`bash $ORACLE_ROOT/bin/checkpoint \"msg\"` does commit + ledger in one step), "
            "next step. Never end a step with a broken tree.\n"
            "4. Evidence standard: a criterion counts only with the command AND its fresh output.\n"
            "5. Re-anchor every ~10 actions: re-read the criteria and your current step.\n"
            "5b. Destructive or hard-to-undo operations (DB mutations, mass deletes/renames): "
            "dry-run first, show the dry-run report, only then execute. Probes are read-only.\n"
            f"6. Finish: full test suite from clean state, commit, ledger entry to "
            f"{MEMORY / 'LEDGER.md'} (what/why/files/next) INCLUDING one 'friction:' line "
            "naming the biggest process obstacle this run (tooling, prompts, missing info) "
            "or 'friction: none' â€” the retrospective mines these to evolve the loop.\n"
            "Finish with EXACTLY ONE status line (seed brain A6), the last line of your "
            "output:\n"
            "  DONE                          all criteria hold with fresh evidence\n"
            "  DONE_WITH_CONCERNS: <worry>   done, but flag what the auditor should probe\n"
            "  NEEDS_CONTEXT: <missing>      the task is under-specified; name exactly what\n"
            "  BLOCKED: <known/ruled out/next>  after two genuinely different failed strategies\n"
            "Bad work reported as DONE poisons the mission's memory; an honest "
            "NEEDS_CONTEXT or BLOCKED is a good outcome."
        )

    # ---- verification stack ------------------------------------------------------
    def run_checks(self, t: Task, wt: Path) -> tuple[bool, str]:
        """Deterministic gates. LLM judgment only begins after exit codes agree."""
        if not t.checks:
            reason = "no deterministic checks declared"
            self.ledger(f"CHECK {t.id}", f"[REFUSED] {reason}")
            return False, reason
        fails = []
        for c in t.checks:
            if not isinstance(c, str) or not c.strip():
                fails.append("empty deterministic check command")
                continue
            r = sh(shell_check(c), cwd=wt, timeout=900)
            status = "OK" if r.returncode == 0 else f"EXIT {r.returncode}"
            self.ledger(f"CHECK {t.id}", f"[{status}] $ {c}")
            if r.returncode != 0:
                fails.append(f"$ {c}\n{r.stdout[-1500:]}")
        return (not fails), "\n\n".join(fails)

    def run_regression_gates(self, current: Task, wt: Path) -> tuple[bool, str]:
        """Run every completed task gate on the candidate before ref movement."""

        initial_head = git(wt, "rev-parse", "HEAD")
        initial_status = git(
            wt,
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
        if (
            initial_head.returncode != 0
            or initial_status.returncode != 0
            or initial_status.stdout.strip()
        ):
            return False, "candidate is not a clean committed tree before regression gates"
        expected_head = initial_head.stdout.strip()
        prior = [
            task
            for task in self.m.tasks
            if task.id != current.id and task.status == "done"
        ]
        for task in prior:
            if not task.checks:
                return False, f"prior task {task.id} has no regression checks"
            for command in task.checks:
                result = sh(shell_check(command), cwd=wt, timeout=900)
                self.ledger(
                    f"REGRESSION CHECK {task.id}",
                    f"[{'OK' if result.returncode == 0 else 'FAIL'}] $ {command}",
                )
                if result.returncode != 0:
                    return (
                        False,
                        f"prior task {task.id} gate failed: {command}\n"
                        f"{result.stdout[-1000:]}",
                    )
                observed_head = git(wt, "rev-parse", "HEAD")
                observed_status = git(
                    wt,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                )
                if (
                    observed_head.returncode != 0
                    or observed_head.stdout.strip() != expected_head
                    or observed_status.returncode != 0
                    or observed_status.stdout.strip()
                ):
                    return (
                        False,
                        f"prior task {task.id} regression check mutated the "
                        f"candidate: {command}",
                    )
        return True, ""

    def audit(self, t: Task, wt: Path, tier: str | None = None, tag: str = "",
              concerns: str = "") -> tuple[bool, str]:
        t.status = "auditing"
        self.write_state()
        diff = ""
        if self.use_git:
            if git(wt, "add", "-A").returncode != 0:
                return False, "FAIL: could not stage audit snapshot"
            dirty = git(wt, "status", "--porcelain")
            if dirty.returncode != 0:
                return False, "FAIL: could not inspect audit snapshot"
            if dirty.stdout.strip():
                commit = git(wt, "commit", "-m", f"wip({t.id}): pre-audit snapshot")
                if commit.returncode != 0:
                    return False, "FAIL: could not commit audit snapshot"
            diff_result = git(wt, "diff", f"{self.branch}...HEAD", "--stat")
            if diff_result.returncode != 0 or not diff_result.stdout.strip():
                return False, "FAIL: task produced no auditable diff"
            diff = diff_result.stdout[-3000:]
        acc = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(t.acceptance)) or "  (none listed)"
        conc = ""
        if concerns:
            conc = (f"\nTHE DEVELOPER FLAGGED CONCERNS (probe these first): {concerns}\n")
        prompt = (
            f"You are the AUDITOR. Verify task [{t.id}] '{t.title}' in the current directory.\n"
            f"ACCEPTANCE CRITERIA:\n{acc}\n{conc}\nCHANGE SUMMARY:\n{diff}\n\n"
            "Verify the ARTIFACT, not the developer's report (seed brain A7): run the tests "
            "yourself, inspect the actual changes. Spec compliance first â€” nothing missing, "
            "nothing extra â€” then quality (A8). Do NOT fix anything.\n"
            "Also flag scope drift (changed files the task did not require) and any test that "
            "was weakened or deleted to force a pass â€” both are FAIL reasons.\n"
            "Finish with exactly one line: 'AUDIT: PASS' or 'AUDIT: FAIL: <reasons>'."
        )
        audit_tag = f"{t.id}-audit{t.attempts}{tag}"
        review_wt, expected_head = self._make_review_worktree(
            wt,
            f"{t.id}-audit{tag or '-primary'}",
        )
        try:
            out = self.run_engine(
                prompt,
                tier or t.audit_tier,
                review_wt,
                25,
                audit_tag,
                stall_min=12,
            )
        finally:
            review_clean = self._finish_review_worktree(
                review_wt,
                expected_head,
            )
        if not review_clean:
            return False, (
                "FAIL: auditor mutation: isolated review artifact was mutated"
            )
        failure = self.engine_failure(audit_tag, out)
        if failure:
            return False, f"FAIL: auditor infrastructure failure: {failure}"
        last_line = out.strip().splitlines()[-1]
        if last_line == "AUDIT: PASS":
            return True, "PASS"
        match = re.fullmatch(r"AUDIT:\s*FAIL:\s*(\S.*)", last_line)
        if match:
            return False, f"FAIL: auditor findings: {match.group(1)}"
        return False, "FAIL: auditor protocol: no exact terminal verdict"

    def adversary_pass(self, t: Task, wt: Path) -> tuple[bool, str]:
        prompt = (
            f"You are the ADVERSARY. Attack the work for task [{t.id}] '{t.title}' in the "
            "current directory: edge cases, statistical validity, silent failures, wasted effort. "
            "Reproduce anything you claim. End with 'ADVERSARY: <c> critical, <m> major, <n> minor'."
        )
        adversary_tag = f"{t.id}-adversary"
        review_wt, expected_head = self._make_review_worktree(
            wt,
            f"{t.id}-adversary",
        )
        try:
            out = self.run_engine(
                prompt,
                "opus",
                review_wt,
                30,
                adversary_tag,
                stall_min=15,
            )
        finally:
            review_clean = self._finish_review_worktree(
                review_wt,
                expected_head,
            )
        if not review_clean:
            verdict = "FAIL: adversary mutated its isolated review artifact"
            self.ledger(f"ADVERSARY on {t.id}", verdict)
            return False, verdict
        failure = self.engine_failure(adversary_tag, out)
        if failure:
            verdict = f"FAIL: adversary infrastructure failure: {failure}"
            self.ledger(f"ADVERSARY on {t.id}", verdict)
            return False, verdict
        last_line = out.strip().splitlines()[-1]
        match = re.fullmatch(
            r"ADVERSARY:\s*(\d+)\s+critical,\s*(\d+)\s+major,\s*(\d+)\s+minor",
            last_line,
            re.I,
        )
        if not match:
            verdict = "FAIL: adversary produced no exact terminal summary"
            self.ledger(f"ADVERSARY on {t.id}", verdict)
            return False, verdict
        critical, major, minor = (int(value) for value in match.groups())
        verdict = (
            f"ADVERSARY: {critical} critical, {major} major, {minor} minor"
        )
        self.ledger(f"ADVERSARY on {t.id}", verdict)
        return critical == 0 and major == 0 and minor == 0, verdict

    # ---- task lifecycle -----------------------------------------------------
    def cutoff(self) -> float:
        """Reserve at most ten minutes, scaled down for short missions."""

        reserve = min(
            600.0,
            max(5.0, float(self.m.hours) * 3600.0 * 0.05),
        )
        return self.deadline - reserve

    def best_of_n_round(self, t: Task, brief: str) -> tuple[bool, bool]:
        """Frontier resampling (seed brain: 'best-of-n plus adversarial
        verification where correctness outranks latency - free with local
        tokens'). Generate N independent candidates from the same base, gate
        each through deterministic checks, audit the survivors, merge the first
        candidate that passes everything. Return (winner, infrastructure_only)."""
        n = t.best_of_n
        self.ledger(f"TOURNAMENT {t.id}", f"best-of-{n}: independent candidates, "
                                          "checks gate, audit picks the winner")
        survivors: list[tuple[int, Path, str, str]] = []
        infrastructure_failure = False
        task_failure = False
        for k in range(1, n + 1):
            if time.monotonic() >= self.cutoff():
                break
            wt, branch = self.make_candidate_worktree(t, k)
            if wt is None:
                raise ConductorError("candidate worktree isolation is unavailable")
            if brief:
                (wt / "RESEARCH.md").write_text(f"# Researcher brief — {t.id}\n\n{brief}\n",
                                                encoding="utf-8")
            candidate_tag = f"{t.id}-cand{k}"
            out = self.run_engine(
                self.developer_prompt(t, "", brief),
                t.tier,
                wt,
                t.timeout_minutes,
                candidate_tag,
                stall_min=t.stall_minutes,
            )
            failure = self.engine_failure(candidate_tag, out)
            if failure:
                infrastructure_failure = True
                self.ledger(f"TOURNAMENT {t.id}", f"candidate {k}: run failed (infra/watchdog)")
                self.drop_worktree(wt, branch)
                continue
            last_line = out.strip().splitlines()[-1]
            concerns = ""
            concern_match = re.fullmatch(
                r"DONE_WITH_CONCERNS:\s*(\S.*)",
                last_line,
            )
            if concern_match:
                concerns = concern_match.group(1)[:400]
            elif last_line != "DONE":
                task_failure = True
                self.ledger(
                    f"TOURNAMENT {t.id}",
                    f"candidate {k}: missing exact terminal status",
                )
                self.drop_worktree(wt, branch)
                continue
            if not self.prepare_verification(t, wt, branch):
                task_failure = True
                self.ledger(
                    f"TOURNAMENT {t.id}",
                    f"candidate {k}: pre-verification snapshot failed",
                )
                continue
            ok, checks_out = self.run_checks(t, wt)
            self.proc("candidate", task=t.id, k=k, checks="pass" if ok else "fail")
            if ok:
                survivors.append((k, wt, branch, concerns))
            else:
                task_failure = True
                self.ledger(f"TOURNAMENT {t.id}", f"candidate {k}: checks failed")
                self.drop_worktree(wt, branch)
        self.ledger(f"TOURNAMENT {t.id}", f"{len(survivors)}/{n} candidates passed checks")
        winner_found = False
        for k, wt, branch, concerns in survivors:
            if not winner_found:
                ok, verdict = self.audit(t, wt, concerns=concerns)
                self.ledger(f"AUDIT {t.id}", f"candidate {k}: {verdict[:200]}")
                if verdict.startswith("FAIL: auditor infrastructure failure:"):
                    infrastructure_failure = True
                    self.drop_worktree(wt, branch)
                    continue
                if ok:
                    if t.adversary:
                        adversary_ok, adversary_verdict = self.adversary_pass(t, wt)
                        if not adversary_ok:
                            if adversary_verdict.startswith(
                                "FAIL: adversary infrastructure failure:"
                            ):
                                infrastructure_failure = True
                            else:
                                task_failure = True
                            self.log_failure(
                                t,
                                "adversary-fail",
                                adversary_verdict,
                            )
                            self.drop_worktree(wt, branch)
                            continue
                    if self.merge_task(t, wt, branch_name=branch):
                        t.status = "done"
                        self.proc("attempt", task=t.id, attempt=t.attempts, tier=t.tier,
                                  escalated=False, outcome="done", tournament=k)
                        self.regression_sweep()
                        winner_found = True
                        continue
                else:
                    task_failure = True
            self.drop_worktree(wt, branch)
        return winner_found, infrastructure_failure and not task_failure

    def run_task(self, t: Task) -> None:
        feedback = ""
        physical_rounds = 0
        # Tournament path: attempt 1 samples N candidates; on a fully failed
        # round the task falls through to the normal retry ladder below.
        if t.best_of_n > 1 and self.use_git:
            t.attempts += 1
            t.status = "running"
            self.current = f"task {t.id} (best-of-{t.best_of_n} tournament, {t.tier})"
            self.write_state()
            brief = ""
            if t.research:
                try:
                    rwt = self.make_worktree(t)
                    brief = self.research_pass(t, rwt)
                except Exception as e:
                    self.ledger(f"RESEARCH ERROR {t.id}", str(e)[:200])
            tournament_winner, tournament_infra = self.best_of_n_round(t, brief)
            physical_rounds += 1
            if tournament_winner:
                return
            if tournament_infra:
                t.attempts -= 1
                t.infra_strikes += 1
                self.proc(
                    "infra",
                    task=t.id,
                    attempt=1,
                    strike=t.infra_strikes,
                    detail="best-of-N verification had infrastructure-only failures",
                )
                feedback = (
                    "The best-of-N round was refunded because only infrastructure "
                    "failures occurred."
                )
            else:
                feedback = ("A best-of-N tournament ran: no candidate passed checks+audit. "
                            "Study FEEDBACK/RESEARCH context and take a fundamentally "
                            "different approach.")
                self.log_failure(t, "tournament", f"best-of-{t.best_of_n}: no candidate survived")
        while t.attempts < t.max_attempts:
            if physical_rounds > 0 and t.requires_approval:
                t.status = "pending"
                t.note = "fresh approval required before another execution attempt"
                self._rotate_approval(t)
                self.write_state()
                return
            physical_rounds += 1
            if time.monotonic() >= self.cutoff():
                t.status, t.note = "pending", "deadline reached before dispatch"
                return
            t.attempts += 1
            tier = t.tier
            if (t.escalate and t.attempts == t.max_attempts
                    and TIER_MODEL["opus"] != TIER_MODEL[t.tier]):
                tier = "opus"
                self.ledger(f"ESCALATE {t.id}", "final attempt runs on the opus tier")
            t.status = "running"
            self.current = f"task {t.id} (attempt {t.attempts}, {tier})"
            self.write_state()
            self.ledger(f"DISPATCH {t.id}", f"attempt {t.attempts}/{t.max_attempts}, tier {tier}")
            attempt_t0 = time.monotonic()
            wt = self.make_worktree(t)
            if feedback:
                (wt / "FEEDBACK.md").write_text(
                    f"# Attempt {t.attempts - 1} failure details\n\n{feedback}\n", encoding="utf-8")
            brief = ""
            if t.research and t.attempts == 1:
                try:
                    brief = self.research_pass(t, wt)
                except Exception as e:            # recon is best-effort, never blocks
                    self.ledger(f"RESEARCH ERROR {t.id}", str(e)[:200])
            # Thinking models emit no stream events mid-thought; give opus runs
            # a longer silence allowance before the stall watchdog fires. Retries
            # also get progressively more slack: if attempt 1 died to the
            # watchdog, killing attempt 2 at the same threshold just repeats the
            # same false-positive kill (seed brain E3).
            stall = max(t.stall_minutes, 20) if tier == "opus" else t.stall_minutes
            stall = int(stall * (1 + 0.5 * (t.attempts - 1)))
            out = self.run_engine(self.developer_prompt(t, feedback, brief), tier, wt,
                                  t.timeout_minutes, f"{t.id}-dev{t.attempts}",
                                  stall_min=stall)
            developer_tag = f"{t.id}-dev{t.attempts}"
            def attempt_end(outcome: str, **kw) -> None:
                self.proc("attempt", task=t.id, attempt=t.attempts, tier=tier,
                          escalated=tier != t.tier, outcome=outcome,
                          secs=round(time.monotonic() - attempt_t0), **kw)

            # Engine/API failures are INFRASTRUCTURE, not task failures: the model
            # never got to work, so the attempt must not count against the task.
            # Heal the serving layer, back off, and retry (bounded).
            stripped = out.strip()
            infrastructure_failure = self.engine_failure(developer_tag, out)
            if infrastructure_failure:
                attempted_number = t.attempts
                t.infra_strikes += 1
                t.attempts -= 1                       # refund the attempt
                self.ledger(f"INFRA {t.id}",
                            f"engine/API failure (strike {t.infra_strikes}/3): "
                            f"{infrastructure_failure[:200]}")
                self.log_failure(t, "infra", infrastructure_failure[:400])
                self.proc(
                    "infra",
                    task=t.id,
                    attempt=attempted_number,
                    strike=t.infra_strikes,
                    detail=infrastructure_failure[:200],
                )
                if t.infra_strikes >= 3:
                    t.status = "blocked"
                    t.note = (
                        f"infrastructure failure x{t.infra_strikes}: "
                        f"{infrastructure_failure[:200]}"
                    )
                    self.ledger(f"BLOCKED {t.id}", "persistent engine/API failure â€” "
                                "check serving context size and engine config")
                    self.write_state()
                    return
                self.ensure_serving()
                self.write_state()
                time.sleep(30)
                continue
            last_line = out.strip().splitlines()[-1] if out.strip() else ""
            if re.fullmatch(r"BLOCKED:\s*\S.*", last_line):
                t.status, t.note = "blocked", last_line[:300]
                self.ledger(f"BLOCKED {t.id}", t.note)
                self.log_failure(t, "blocked", t.note)
                attempt_end("blocked")
                return
            if re.fullmatch(r"NEEDS_CONTEXT:\s*\S.*", last_line):
                # A6/A5: under-specified task. Retrying the same prompt harder is
                # waste â€” surface it for the planner/operator instead.
                t.status, t.note = "blocked", last_line[:300]
                self.ledger(f"NEEDS_CONTEXT {t.id}", t.note)
                self.log_failure(t, "needs-context", t.note)
                attempt_end("needs-context")
                return
            concerns = ""
            m_conc = re.fullmatch(
                r"DONE_WITH_CONCERNS:\s*(\S.*)",
                last_line,
            )
            if m_conc:
                concerns = m_conc.group(1).strip()[:400]
                self.ledger(f"CONCERNS {t.id}", concerns or "(unspecified)")
            elif last_line != "DONE":
                feedback = (
                    "Developer output omitted the exact terminal status line; "
                    "no verification gate was run."
                )
                self.log_failure(t, "missing-status", last_line[:400])
                attempt_end("missing-status")
                continue
            if not self.prepare_verification(t, wt):
                feedback = (
                    "Could not bind the candidate to the current mission tip "
                    f"before verification: {t.note}"
                )
                self.log_failure(t, "verification-prepare", feedback)
                attempt_end("verification-prepare")
                continue
            checks_ok, checks_out = self.run_checks(t, wt)
            if not checks_ok:
                self.log_failure(t, "checks-fail", checks_out[:600])
                feedback = f"Deterministic checks failed:\n{checks_out}"
                attempt_end("checks-fail")
                continue
            ok, verdict = self.audit(t, wt, concerns=concerns)
            if verdict.startswith("FAIL: auditor infrastructure failure:"):
                attempted_number = t.attempts
                t.infra_strikes += 1
                t.attempts -= 1
                self.log_failure(t, "infra", verdict)
                self.proc(
                    "infra",
                    task=t.id,
                    attempt=attempted_number,
                    strike=t.infra_strikes,
                    detail=verdict[:200],
                )
                if t.infra_strikes >= 3:
                    t.status = "blocked"
                    t.note = (
                        f"infrastructure failure x{t.infra_strikes}: "
                        f"{verdict[:200]}"
                    )
                    self.write_state()
                    return
                self.ensure_serving()
                self.write_state()
                time.sleep(30)
                continue
            tiebreak = False
            if (
                not ok
                and verdict.startswith("FAIL: auditor findings:")
                and t.checks
                and TIER_MODEL["opus"] != TIER_MODEL[t.audit_tier]
            ):
                # Checks passed but the auditor disagrees â€” tiebreak with the
                # strongest model (skipped when the profile maps opus to the same
                # model, where a re-audit would add nothing).
                ok2, verdict2 = self.audit(t, wt, tier="opus", tag="-tiebreak")
                if verdict2.startswith("FAIL: auditor infrastructure failure:"):
                    attempted_number = t.attempts
                    t.infra_strikes += 1
                    t.attempts -= 1
                    self.log_failure(t, "infra", verdict2)
                    self.proc(
                        "infra",
                        task=t.id,
                        attempt=attempted_number,
                        strike=t.infra_strikes,
                        detail=verdict2[:200],
                    )
                    if t.infra_strikes >= 3:
                        t.status = "blocked"
                        t.note = (
                            f"infrastructure failure x{t.infra_strikes}: "
                            f"{verdict2[:200]}"
                        )
                        self.write_state()
                        return
                    self.ensure_serving()
                    self.write_state()
                    time.sleep(30)
                    continue
                if ok2:
                    ok, verdict = True, verdict2 + " [opus tiebreak overrode initial FAIL]"
                    tiebreak = True
            self.ledger(f"AUDIT {t.id}", verdict[:300])
            if ok:
                if t.adversary:
                    adversary_ok, adversary_verdict = self.adversary_pass(t, wt)
                    if not adversary_ok:
                        if adversary_verdict.startswith(
                            "FAIL: adversary infrastructure failure:"
                        ):
                            attempted_number = t.attempts
                            t.infra_strikes += 1
                            t.attempts -= 1
                            self.log_failure(t, "infra", adversary_verdict)
                            self.proc(
                                "infra",
                                task=t.id,
                                attempt=attempted_number,
                                strike=t.infra_strikes,
                                detail=adversary_verdict[:200],
                            )
                            if t.infra_strikes >= 3:
                                t.status = "blocked"
                                t.note = (
                                    f"infrastructure failure x{t.infra_strikes}: "
                                    f"{adversary_verdict[:200]}"
                                )
                                self.write_state()
                                return
                            self.ensure_serving()
                            self.write_state()
                            time.sleep(30)
                            continue
                        self.log_failure(
                            t,
                            "adversary-fail",
                            adversary_verdict,
                        )
                        attempt_end("adversary-fail")
                        feedback = adversary_verdict
                        continue
                if self.merge_task(t, wt):
                    t.status = "done"
                    attempt_end("done", tiebreak=tiebreak)
                    self.regression_sweep()
                else:
                    outcome = (
                        "regression-fail"
                        if t.status == "regression-failed"
                        else "merge-conflict"
                    )
                    attempt_end(outcome)
                    if t.status == "regression-failed":
                        feedback = t.note
                        continue
                return
            self.log_failure(t, "audit-fail", verdict)
            attempt_end("audit-fail")
            feedback = verdict
        t.status, t.note = "failed", f"failed audit {t.max_attempts} times: {feedback[:200]}"
        self.ledger(f"FAILED {t.id}", t.note)
        self.log_failure(t, "exhausted", t.note)

    # ---- approvals (guardian countersign for irreversible work) ---------------
    def _challenge_for(self, t: Task) -> dict[str, Any]:
        if not t.requires_approval:
            raise ConductorError(f"task {t.id} does not require approval")
        challenge = self.approval_challenges.get(t.id)
        expected_digest = _task_spec_digest(t)
        if (
            not isinstance(challenge, dict)
            or challenge.get("task_digest") != expected_digest
            or challenge.get("used") is True
        ):
            previous_generation = (
                challenge.get("generation", 0)
                if isinstance(challenge, dict)
                and isinstance(challenge.get("generation"), int)
                else 0
            )
            challenge = _new_approval_challenge(
                t,
                generation=previous_generation + 1,
            )
            self.approval_challenges[t.id] = challenge
        return challenge

    def _rotate_approval(self, t: Task) -> None:
        if not t.requires_approval:
            return
        previous = self.approval_challenges.get(t.id, {})
        generation = (
            previous.get("generation", 0)
            if isinstance(previous, dict)
            and isinstance(previous.get("generation"), int)
            else 0
        )
        self.approval_challenges[t.id] = _new_approval_challenge(
            t,
            generation=generation + 1,
        )

    def approval_command(self, t: Task) -> str:
        challenge = self._challenge_for(t)
        return (
            f"APPROVE {self.run_id} {t.id} "
            f"{challenge['generation']} {challenge['task_digest']} "
            f"{challenge['nonce']}"
        )

    def denial_command(self, t: Task) -> str:
        challenge = self._challenge_for(t)
        return (
            f"DENY {self.run_id} {t.id} "
            f"{challenge['generation']} {challenge['task_digest']} "
            f"{challenge['nonce']}"
        )

    def _operator_decisions(self) -> set[str]:
        path = MEMORY / "APPROVALS.md"
        if not path.exists():
            atomic_write_text(
                path,
                "# Operator decisions - append the exact APPROVE or DENY "
                "challenge shown in PENDING-APPROVALS.json.\n\n",
            )
            return set()
        return {
            line.strip()
            for line in path.read_text(
                encoding="utf-8",
                errors="strict",
            ).splitlines()
        }

    def approved(self, t: Task) -> bool:
        """Require the exact run/task/nonce challenge for irreversible work."""

        if not t.requires_approval:
            return True
        challenge = self._challenge_for(t)
        if challenge["used"]:
            return False
        expected = self.approval_command(t)
        return expected in self._operator_decisions()

    def denied(self, t: Task) -> bool:
        if not t.requires_approval:
            return False
        challenge = self._challenge_for(t)
        if challenge["used"]:
            return False
        return self.denial_command(t) in self._operator_decisions()

    def _consume_approval(self, t: Task) -> None:
        if not t.requires_approval:
            return
        challenge = self._challenge_for(t)
        if not self.approved(t):
            raise ConductorError(f"task {t.id} approval is absent or stale")
        challenge["used"] = True

    def awaiting_approval(self) -> list[Task]:
        done = {t.id for t in self.m.tasks if t.status == "done"}
        out = []
        for t in self.m.tasks:
            if (t.status == "pending" and t.requires_approval and not self.approved(t)
                    and all(d in done for d in t.depends_on)):
                out.append(t)
        return out

    # ---- scheduling -----------------------------------------------------------
    def claim(self, background: bool, fast_only: bool = False) -> Task | None:
        """Atomically pick a dispatchable task and mark it claimed.
        Ledger events are emitted after the state lock is released."""
        approval_events: list[tuple[str, str]] = []
        denial_events: list[tuple[str, str]] = []
        picked: Task | None = None
        with self.lock:
            done = {t.id for t in self.m.tasks if t.status == "done"}
            for t in self.m.tasks:
                if t.background != background or t.status != "pending":
                    continue
                if fast_only and t.tier != "haiku":
                    continue
                if not all(d in done for d in t.depends_on):
                    continue
                if self.denied(t):
                    self._challenge_for(t)["used"] = True
                    t.status = "blocked"
                    t.note = "operator denied the exact irreversible-task challenge"
                    self.write_checkpoint()
                    denial_events.append((t.id, t.note))
                    continue
                if not self.approved(t):
                    if "awaiting operator approval" not in t.note:
                        t.note = (
                            "awaiting operator approval (append exact challenge: "
                            f"{self.approval_command(t)})"
                        )
                        approval_events.append((t.id, t.note))
                    continue
                self._consume_approval(t)
                t.status = "claimed"
                t.note = ""
                self.write_checkpoint()
                picked = t
                break
        for tid, note in approval_events:
            self.ledger(f"APPROVAL NEEDED {tid}", note)
        for tid, note in denial_events:
            self.ledger(f"APPROVAL DENIED {tid}", note)
        return picked

    def dispatchable(self, background: bool) -> Task | None:
        """Read-only view (used by reports)."""
        done = {t.id for t in self.m.tasks if t.status == "done"}
        for t in self.m.tasks:
            if (t.background == background and t.status == "pending"
                    and all(d in done for d in t.depends_on)):
                return t
        return None

    def regression_sweep(self) -> None:
        """After every merge, re-run the checks of ALL previously-done tasks against
        the advanced mission branch. A later merge that breaks an earlier gate
        REOPENS that task (with one repair attempt). This is what keeps 'done'
        meaning done for the whole mission, not just at the moment of merge."""
        if not self.use_git:
            return
        prior = [x for x in self.m.tasks if x.status == "done" and x.checks]
        if not prior:
            return
        with self.sweeplock:
            expected = git(self.m.repo, "rev-parse", self.branch)
            if (
                expected.returncode != 0
                or not re.fullmatch(
                    r"[0-9a-f]{40,64}",
                    expected.stdout.strip(),
                )
            ):
                raise ConductorError("regression sweep could not bind mission head")
            wt = WORKTREES / (
                f"{self.m.name}-gates-{self.run_id[:8]}-"
                f"{secrets.token_hex(4)}"
            )
            with self.gitlock:
                r = git(
                    self.m.repo,
                    "worktree",
                    "add",
                    "--detach",
                    str(wt),
                    self.branch,
                )
                if r.returncode != 0:
                    raise ConductorError(
                        "regression worktree creation failed: "
                        f"{r.stdout[-300:]}"
                    )
                self.owned_worktrees.add(wt)
            try:
                reopened = []
                for x in prior:
                    for c in x.checks:
                        result = sh(shell_check(c), cwd=wt, timeout=900)
                        observed_head = git(wt, "rev-parse", "HEAD")
                        observed_status = git(
                            wt,
                            "status",
                            "--porcelain",
                            "--untracked-files=all",
                        )
                        if (
                            observed_head.returncode != 0
                            or observed_head.stdout.strip()
                            != expected.stdout.strip()
                            or observed_status.returncode != 0
                            or observed_status.stdout.strip()
                        ):
                            raise ConductorError(
                                f"regression check mutated isolated artifact: {c}"
                            )
                        if result.returncode != 0:
                            with self.lock:
                                x.status = "pending"
                                x.attempts = min(
                                    x.attempts,
                                    x.max_attempts - 1,
                                )
                                x.note = (
                                    "REGRESSION: gate broke after a later merge: "
                                    f"{c}"
                                )
                                self.task_commits.pop(x.id, None)
                                self._rotate_approval(x)
                            self.log_failure(
                                x,
                                "regression",
                                f"$ {c}\n{result.stdout[-500:]}",
                            )
                            self.proc("regression", task=x.id, check=c)
                            reopened.append(x.id)
                            break
                current = git(self.m.repo, "rev-parse", self.branch)
                if (
                    current.returncode != 0
                    or current.stdout.strip() != expected.stdout.strip()
                ):
                    raise ConductorError(
                        "mission branch advanced during regression sweep"
                    )
                if reopened:
                    self.ledger(
                        "REGRESSION",
                        "reopened: " + ", ".join(reopened),
                    )
                    self.write_checkpoint()
                else:
                    self.ledger(
                        "GATES",
                        f"regression sweep clean ({len(prior)} prior tasks)",
                    )
            finally:
                status = git(
                    wt,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                )
                observed = git(wt, "rev-parse", "HEAD")
                if (
                    status.returncode == 0
                    and not status.stdout.strip()
                    and observed.returncode == 0
                    and observed.stdout.strip() == expected.stdout.strip()
                ):
                    with self.gitlock:
                        removed = git(
                            self.m.repo,
                            "worktree",
                            "remove",
                            str(wt),
                        )
                    if removed.returncode != 0:
                        raise ConductorError(
                            "regression worktree cleanup failed; artifact preserved"
                        )
                    self.owned_worktrees.discard(wt)
                else:
                    self.ledger(
                        "GATES MUTATION",
                        f"regression check mutated isolated artifact; preserved: {wt}",
                    )

    def all_terminal(self) -> bool:
        return all(t.status in ("done", "failed", "blocked", "merge-conflict")
                   for t in self.m.tasks)

    def anything_active(self) -> bool:
        return any(t.status in ("claimed", "running", "auditing") for t in self.m.tasks)

    def fast_worker(self) -> None:
        """Second worker: drives haiku-tier tasks on the always-resident fast lane,
        in parallel with the big slot. Worktrees isolate; gitlock serializes merges."""
        while not self.stopping.is_set() and time.monotonic() < self.cutoff():
            t = self.claim(False, fast_only=True) or self.claim(True, fast_only=True)
            if not t:
                self.stopping.wait(20)
                continue
            self.ledger(f"FAST-LANE {t.id}", "picked up by the parallel fast worker")
            try:
                self.run_task(t)
            except BaseException as exc:
                self.fatal_error = exc
                self.stopping.set()
                return

    # ---- compounding memory ---------------------------------------------------
    def distill_lessons(self) -> None:
        """End-of-mission: distill the ledger + failure memory into a few reusable
        lessons in memory/LESSONS.md â€” the mechanism that makes mission N+1 start
        smarter than mission N (the 'do not relearn these' file)."""
        if not any(t.attempts for t in self.m.tasks):
            return
        def tail(p: Path, n: int) -> str:
            return p.read_text(encoding="utf-8", errors="replace")[-n:] if p.exists() else ""
        prompt = (
            "You are the mission historian. From the records below, distill 3-8 numbered "
            "LESSONS for future missions in this repository: reusable facts, gotchas, "
            "commands that work, approaches that are proven dead. One line each, "
            "imperative, general (no mission-specific trivia).\n"
            "Tag every lesson: if it instantiates a seed-brain principle from the index "
            "below, end it with ' [seed: <ID>]'; if it GENERALIZES beyond this repository "
            "and no existing principle covers it, end it with ' [CANDIDATE-PRINCIPLE: "
            "<series letter O/A/L/C/E/V/G/M>]' â€” the retrospective promotes candidates "
            "into the founding memory. Respond with ONLY the numbered list.\n\n"
            f"SEED-BRAIN INDEX:\n{seed_brain_index()}\n\n"
            f"LEDGER (tail):\n{tail(MEMORY / 'LEDGER.md', 6000)}\n\n"
            f"FAILURE MEMORY (tail):\n{tail(MEMORY / 'FAILURES.md', 4000)}"
        )
        out = self.run_readonly_engine(
            prompt,
            "haiku",
            self.m.repo,
            10,
            "lessons",
            stall_min=8,
        ).strip()
        lines = [ln.strip() for ln in out.splitlines()
                 if re.match(r"^\d+[.)]\s+\S", ln.strip())][:8]
        if not lines:
            return
        p = MEMORY / "LESSONS.md"
        with self.lock:
            if not p.exists():
                p.write_text(
                    "# Lessons â€” Layer 1 runtime memory (this machine's distilled "
                    "experience).\n# Layer 0 (founding memory) is engines/shared/"
                    "SEED-BRAIN.md â€” stable principles with IDs.\n# Read during the "
                    "start ritual; do not relearn these. Lessons tagged "
                    "[CANDIDATE-PRINCIPLE] are\n# promoted into the seed brain by the "
                    "retrospective's amendment flow.\n\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as f:
                f.write(f"## {now()} Â· mission {self.m.name}\n" + "\n".join(lines) + "\n\n")
        self.ledger("LESSONS", f"{len(lines)} lessons distilled to memory/LESSONS.md")

    # ---- the evolving loop (retrospective -> amendments -> measurement) --------
    def _mission_metrics(self) -> tuple[dict, dict]:
        """Aggregate PROCESS.jsonl into per-mission process metrics; return
        (current_mission_metrics, previous_mission_metrics)."""
        path = MEMORY / "PROCESS.jsonl"
        per: dict[str, list[dict]] = {}
        order: list[str] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                name = rec.get("mission", "?")
                if name not in per:
                    per[name] = []
                    order.append(name)
                per[name].append(rec)

        def agg(recs: list[dict]) -> dict:
            att = [r for r in recs if r.get("kind") == "attempt"]
            outcomes = [r.get("outcome") for r in att]
            done_tasks = {r["task"] for r in att if r.get("outcome") == "done"}
            per_task_attempts = {}
            for r in att:
                per_task_attempts[r["task"]] = max(per_task_attempts.get(r["task"], 0),
                                                   int(r.get("attempt", 1)))
            first_try = sum(1 for tsk in done_tasks
                            if any(r["task"] == tsk and r.get("outcome") == "done"
                                   and int(r.get("attempt", 9)) == 1 for r in att))
            return {
                "attempts": len(att),
                "tasks_done": len(done_tasks),
                "first_attempt_pass_rate": round(first_try / len(done_tasks), 2) if done_tasks else None,
                "mean_attempts_per_done_task": round(
                    sum(per_task_attempts[tsk] for tsk in done_tasks) / len(done_tasks), 2)
                    if done_tasks else None,
                "outcome_counts": {o: outcomes.count(o) for o in set(outcomes) if o},
                "watchdog_kills": outcomes.count("watchdog"),
                "checks_failures": outcomes.count("checks-fail"),
                "audit_failures": outcomes.count("audit-fail"),
                "tiebreak_overrides": sum(1 for r in att if r.get("tiebreak")),
                "escalated_attempts": sum(1 for r in att if r.get("escalated")),
                "regressions_reopened": sum(1 for r in recs if r.get("kind") == "regression"),
                "infra_events": sum(1 for r in recs if r.get("kind") == "infra"),
                "overseer_concerns": sum(1 for r in recs if r.get("kind") == "overseer"
                                         and "CONCERN" in str(r.get("verdict", ""))),
                "mean_attempt_secs": round(sum(int(r.get("secs", 0)) for r in att) / len(att))
                    if att else None,
            }

        cur = agg(per.get(self.m.name, []))
        prev_names = [n for n in order if n != self.m.name]
        prev = agg(per[prev_names[-1]]) if prev_names else {}
        if prev_names:
            prev["mission"] = prev_names[-1]
        return cur, prev

    def retrospective(self) -> None:
        """Meta-analysis of the loop's own process, producing pre-registered
        protocol amendments. Lifecycle: PROPOSED (here) -> operator countersign ->
        APPLIED (by an approval-gated amendment mission, verified by the normal
        loop) -> MEASURED at the next retro against its success criterion ->
        kept, extended, or reverted. The loop evolves itself through itself."""
        cur, prev = self._mission_metrics()
        if not cur.get("attempts"):
            return
        self.current = "retrospective: meta-analyzing this mission's process"
        self.ledger("RETRO", "meta-analyzing process telemetry")

        def tail(p: Path, n: int) -> str:
            return p.read_text(encoding="utf-8", errors="replace")[-n:] if p.exists() else ""
        friction = "\n".join(ln for ln in tail(MEMORY / "LEDGER.md", 8000).splitlines()
                             if "friction:" in ln.lower())[-1500:]
        amendments = tail(MEMORY / "AMENDMENTS.md", 4000)
        candidates = "\n".join(ln for ln in tail(MEMORY / "LESSONS.md", 6000).splitlines()
                               if "CANDIDATE-PRINCIPLE" in ln)[-1200:]
        tier = "opus" if TIER_MODEL["opus"] != TIER_MODEL["sonnet"] else "sonnet"
        date = dt.datetime.now().strftime("%Y%m%d")
        prompt = (
            "You are the PROCESS META-ANALYST for an autonomous engineering loop. Your "
            "subject is the LOOP ITSELF â€” how it plans, works, documents, audits, retries, "
            "and merges â€” not the engineering artifacts it produced.\n"
            "You are STRICTLY READ-ONLY: edit no files, run no mutations â€” your entire "
            "output is this report. Changes happen only through the governed amendment "
            "mission after operator approval.\n\n"
            f"PROCESS METRICS, this mission:\n{json.dumps(cur, indent=1)}\n\n"
            f"PROCESS METRICS, previous mission (baseline):\n{json.dumps(prev, indent=1)}\n\n"
            f"AGENT-REPORTED FRICTION (from ledger entries):\n{friction or '(none reported)'}\n\n"
            f"FAILURE MEMORY (tail):\n{tail(MEMORY / 'FAILURES.md', 3000)}\n\n"
            f"PRIOR AMENDMENTS AND THEIR SUCCESS CRITERIA:\n{amendments or '(none yet)'}\n\n"
            f"CANDIDATE PRINCIPLES flagged by the historian:\n{candidates or '(none)'}\n\n"
            f"SEED-BRAIN INDEX (founding memory, engines/shared/SEED-BRAIN.md):\n"
            f"{seed_brain_index()}\n\n"
            "Produce:\n"
            "1. METRICS READING â€” what moved vs baseline and what it means (be skeptical; "
            "small samples prove little).\n"
            "2. VERDICTS on every prior APPLIED amendment: MET / NOT-MET / "
            "INSUFFICIENT-DATA against its registered success criterion, with the number "
            "that decides it, and an action (keep / revert / extend).\n"
            "3. TOP PROCESS BOTTLENECKS (max 3) with evidence from the data above.\n"
            "4. AMENDMENT PROPOSALS (0-3; propose NOTHING if the evidence is weak â€” "
            "protocol churn is itself a process failure). Each proposal targets exactly "
            "one file among: engines/shared/AUTONOMY.md, engines/shared/CONVENTIONS.md, "
            "engines/shared/SEED-BRAIN.md, skills/*/SKILL.md, or a conductor default, and "
            "must be a small, precise change with a MEASURABLE success criterion "
            "computable from the process metrics above at the next retro.\n"
            "5. PRINCIPLE PROMOTIONS (0-2): incidents from THIS mission whose lesson "
            "GENERALIZES beyond this repository (seed brain M4/M5). Formulate each exactly "
            "like a seed-brain principle â€” project specifics removed, the failure kernel "
            "kept, one imperative rule â€” and emit it as a proposal whose target is "
            "engines/shared/SEED-BRAIN.md and whose change is: append under '## NEW "
            "PRINCIPLES' the line '**<next free ID in its series>.** `[strong]` <text> "
            "(incident: <mission/task>, " + date + ")'. Never renumber or edit existing "
            "principles; if an incident CONTRADICTS one, propose an ERRATA entry instead.\n\n"
            "End with a single ```json block:\n"
            '{"verdicts": [{"id": "AMD-...", "verdict": "MET|NOT-MET|INSUFFICIENT-DATA", '
            '"evidence": "...", "action": "keep|revert|extend"}], '
            '"proposals": [{"id": "AMD-' + date + '-1", "target": "<file>", '
            '"change": "<exact, self-contained edit specification>", '
            '"rationale": "<tied to evidence>", '
            '"success_criterion": "<metric comparison at next retro>"}]}'
        )
        out = self.run_readonly_engine(
            prompt,
            tier,
            ROOT,
            25,
            f"retro-{date}",
            stall_min=15,
        )
        atomic_write_text(
            REPORTS / f"RETRO-{dt.datetime.now():%Y%m%d-%H%M}.md",
            f"# Retrospective â€” mission `{self.m.name}`\n\n{now()}\n\n{out}\n",
        )

        payload: dict = {}
        for block in reversed(re.findall(r"```json\s*(.*?)```", out, re.S)):
            try:
                cand = json.loads(block)
                if isinstance(cand, dict):
                    payload = cand
                    break
            except ValueError:
                continue
        verdicts = payload.get("verdicts") or []
        proposals = []
        allowed = ("engines/shared/", "skills/", "conductor/", "connectors/", "bootstrap/")
        for i, p in enumerate(payload.get("proposals") or [], start=1):
            if not isinstance(p, dict) or not p.get("change") or not p.get("target"):
                continue
            p["id"] = p.get("id") if re.match(r"^AMD-[\w-]+$", str(p.get("id", ""))) \
                else f"AMD-{date}-{i}"
            if str(p["target"]).replace("\\", "/").startswith(allowed):
                proposals.append(p)

        ap = MEMORY / "AMENDMENTS.md"
        with self.lock:
            if not ap.exists():
                ap.write_text("# Protocol amendments â€” PROPOSED by retrospectives, APPLIED by "
                              "approval-gated amendment missions, MEASURED at the next retro.\n\n",
                              encoding="utf-8")
            with ap.open("a", encoding="utf-8") as f:
                for v in verdicts:
                    f.write(f"- VERDICT {now()}: {v.get('id')} -> {v.get('verdict')} "
                            f"({v.get('evidence', '')[:200]}) action={v.get('action')}\n")
                for p in proposals:
                    f.write(f"\n## {p['id']} Â· PROPOSED Â· {now()}\n"
                            f"target: {p['target']}\n"
                            f"change: {p['change']}\n"
                            f"rationale: {p.get('rationale', '')}\n"
                            f"success_criterion: {p.get('success_criterion', '')}\n")
        if proposals:
            mission_file = self._write_amendment_mission(proposals, date)
            self.ledger("RETRO PROPOSALS", f"{len(proposals)} amendment(s) proposed; review "
                        f"memory/AMENDMENTS.md then run: oracle mission {mission_file} "
                        f"(each task needs an APPROVE line first)")
        else:
            self.ledger("RETRO", "no amendments proposed (evidence too weak or process healthy)")

    def _write_amendment_mission(self, proposals: list[dict], date: str) -> str:
        lines = [
            "# AUTO-GENERATED by the retrospective â€” protocol amendment mission.",
            "# Specs live in memory/AMENDMENTS.md. Approve what you accept:",
            "#   run the mission, then append its exact challenge from",
            "#   memory/PENDING-APPROVALS.json to memory/APPROVALS.md",
            "",
            "[mission]",
            f'name = "amendments-{date}"',
            'goal = "Apply approved protocol amendments exactly as specified in memory/AMENDMENTS.md, with a dated Amendment Log entry for each."',
            f'engine = "{self.m.engine}"',
            "hours = 4",
            "",
        ]
        amendments_abs = (MEMORY / "AMENDMENTS.md").as_posix()
        for p in proposals:
            tid = p["id"].lower()
            lines += [
                "[[tasks]]",
                f"id = '{tid}'",
                f"title = 'Apply {p['id']} to {p['target']}'",
                "prompt = '''Apply protocol amendment " + p["id"] + ": read its full "
                "specification in " + amendments_abs + " and implement it EXACTLY on its "
                "target file - nothing beyond the spec. Then (1) append a dated entry to "
                "the Amendment Log at the bottom of engines/shared/AUTONOMY.md (id, "
                "one-line change, success criterion); (2) append the line "
                + p["id"] + " APPLIED to " + amendments_abs + ".'''",
                "acceptance = ['amendment applied verbatim to its target file', "
                "'Amendment Log entry added', 'AMENDMENTS.md marked APPLIED']",
                f"checks = ['grep -q {p['id']} engines/shared/AUTONOMY.md', "
                f"'grep -q \"{p['id']} APPLIED\" {amendments_abs}']",
                "tier = 'sonnet'",
                "requires_approval = true",
                "timeout_minutes = 20",
                "",
            ]
        path = ROOT / "conductor" / "missions" / f"amendments-{date}.toml"
        path.write_text("\n".join(lines), encoding="utf-8")
        return f"conductor/missions/amendments-{date}.toml"

    # ---- overseer (continuous time-use audit) --------------------------------
    def overseer(self) -> str:
        """The original contract: 'auditor agents must monitor and ensure we are
        making effective use of time at all times.' Every report interval, a
        fast-lane, read-only pass judges how the mission is spending its time:
        spinning tasks, repeated failures, idle capacity, drift from the goal.
        The verdict lands in the ledger, the telemetry, and the hourly report."""
        def tail(p: Path, n: int) -> str:
            return p.read_text(encoding="utf-8", errors="replace")[-n:] if p.exists() else ""
        table = "\n".join(f"- {t.id}: {t.status}, attempts {t.attempts}/{t.max_attempts}"
                          f" ({t.note[:100]})" for t in self.m.tasks)
        elapsed = (time.monotonic() - self.t0) / 3600
        prompt = (
            "You are the mission OVERSEER â€” the time-use auditor. STRICTLY READ-ONLY: "
            "modify nothing; your entire output is this judgment.\n\n"
            f"MISSION GOAL: {self.m.goal}\n"
            f"ELAPSED: {elapsed:.1f} h of {self.m.hours} h budget\n"
            f"NOW: {self.current}\n\nTASKS:\n{table}\n\n"
            f"LEDGER (tail):\n{tail(MEMORY / 'LEDGER.md', 4000)}\n\n"
            f"FAILURE MEMORY (tail):\n{tail(MEMORY / 'FAILURES.md', 2000)}\n\n"
            "Judge, in at most 8 lines: (1) is machine time being used effectively "
            "right now, at this burn rate? (2) is any task spinning â€” repeated attempts "
            "hitting the same wall â€” that should be BLOCKED or rerouted instead of "
            "retried? (3) what is the single highest-value next action for the loop? "
            "Then end with EXACTLY one line: 'OVERSEER: OK' or "
            "'OVERSEER: CONCERN: <the one thing to change>'."
        )
        out = self.run_readonly_engine(
            prompt,
            "haiku",
            self.m.repo,
            8,
            f"overseer-{dt.datetime.now():%H%M}",
            stall_min=6,
        ).strip()
        m = re.findall(r"OVERSEER:.*", out)
        if m:
            verdict = m[-1][:300]
        elif not out or "[WATCHDOG]" in out:
            # single-slot boxes: the dev run can hold the model for the whole
            # interval; a starved overseer is a capacity fact, not a concern
            verdict = "OVERSEER: skipped (no engine capacity this interval)"
        else:
            verdict = "OVERSEER: no verdict produced"
        self.ledger("OVERSEER", verdict)
        self.proc("overseer", verdict=verdict)
        return out

    # ---- reporting ----------------------------------------------------------
    def report(self, final: bool = False, overseer_text: str = "") -> Path:
        counts: dict[str, int] = {}
        for t in self.m.tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        elapsed = (time.monotonic() - self.t0) / 3600
        name = "FINAL-REPORT.md" if final else f"REPORT-{dt.datetime.now():%Y%m%d-%H%M}.md"
        lines = [
            f"# {'Final report' if final else 'Hourly report'} â€” mission `{self.m.name}`",
            f"\n{now()} Â· engine {self.m.engine} Â· elapsed {elapsed:.1f} h of {self.m.hours} h",
            f"\n**Happening now:** {self.current}",
            f"\n**Status:** " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
            "\n## Tasks\n| id | status | attempts | note |\n|---|---|---|---|",
        ]
        lines += [f"| {t.id} | {t.status} | {t.attempts} | {t.note} |" for t in self.m.tasks]
        waiting = self.awaiting_approval()
        if waiting:
            lines += [
                "\n**AWAITING OPERATOR APPROVAL** (append the exact "
                "run/task/nonce challenge from "
                "`memory/PENDING-APPROVALS.json`): "
                + ", ".join(t.id for t in waiting)
            ]
        nr = MEMORY / "NET-REQUESTS.md"
        if nr.exists():
            open_reqs = sum(1 for ln in nr.read_text(encoding="utf-8", errors="replace").splitlines()
                            if ln.strip().startswith("- [ ]"))
            if open_reqs:
                lines += [f"\n**OPEN NETWORK REQUESTS:** {open_reqs} â€” run `oracle envoy --queue` "
                          "to fulfil them in a controlled window"]
        nxt = self.dispatchable(False)
        lines += [f"\n**Planned next:** {nxt.id + ' â€” ' + nxt.title if nxt else 'nothing pending'}"]
        if overseer_text:
            lines += ["\n## Overseer â€” time-use audit\n", overseer_text]
        try:
            tail = (MEMORY / "LEDGER.md").read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            lines += ["\n## Ledger tail", *tail]
        except FileNotFoundError:
            pass
        p = REPORTS / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def _report_thread(self) -> None:
        while not self.stopping.wait(self.m.report_minutes * 60):
            osr = ""
            try:
                osr = self.overseer()          # best-effort; the report never blocks on it
            except Exception as e:
                self.ledger("OVERSEER ERROR", str(e)[:200])
            p = self.report(overseer_text=osr)
            self.ledger("HOURLY REPORT", rel_to_root(p))

    # ---- main loop ----------------------------------------------------------
    def run(self) -> int:
        self.ledger("MISSION START",
                    f"goal='{self.m.goal}' engine={self.m.engine} budget={self.m.hours}h "
                    f"tasks={len(self.m.tasks)} workers={self.m.workers} repo={self.m.repo}")
        if not self.use_git:
            self.ledger("WARN", "target repo is not a git repo â€” running without worktree isolation")
        try:
            self.ensure_tools()                    # missing tools are healed, not fatal
        except Exception as e:
            self.ledger("TOOLBELT ERROR", str(e)[:200])
        if self.m.auto_plan and not [t for t in self.m.tasks if not t.background]:
            if not self.plan_mission():
                self.ledger("MISSION END", "planning failed; nothing to execute")
                return 1
        worker_threads = [
            threading.Thread(
                target=self._report_thread,
                name=f"conductor-report-{self.run_id[:8]}",
            )
        ]
        if self.m.workers > 1:
            worker_threads.append(
                threading.Thread(
                    target=self.fast_worker,
                    name=f"conductor-fast-{self.run_id[:8]}",
                )
            )
        for worker_thread in worker_threads:
            worker_thread.start()
        try:
            while time.monotonic() < self.cutoff():
                if self.fatal_error is not None:
                    raise ConductorError(
                        f"parallel worker failed: {self.fatal_error}"
                    ) from self.fatal_error
                self.write_state()
                t = self.claim(False) or self.claim(True)
                if t:
                    self.run_task(t)
                    continue
                if self.anything_active():        # parallel worker is mid-task
                    time.sleep(15)
                    continue
                if self.all_terminal():
                    if all(
                        task.status == "done"
                        for task in self.m.tasks
                        if not task.background
                    ):
                        self.regression_sweep()
                        if any(
                            task.status == "pending"
                            for task in self.m.tasks
                        ):
                            continue
                    self.ledger("MISSION COMPLETE", "no pending tasks remain")
                    break
                # Dispatchable-but-unapproved tasks: hold the mission open for the
                # operator's countersign instead of treating it as a stall.
                waiting = self.awaiting_approval()
                if waiting:
                    self.current = ("waiting for operator approval: "
                                    + ", ".join(t.id for t in waiting))
                    time.sleep(60)
                    continue
                # Pending tasks exist but none are dispatchable: dependencies failed.
                if self.m.auto_plan and not self.replanned:
                    self.replanned = True
                    if self.replan():
                        continue
                pending = [t for t in self.m.tasks if t.status == "pending"]
                self.current = f"stalled â€” {len(pending)} tasks blocked by failed dependencies"
                self.ledger("STALLED", self.current + " â€” ending early rather than idling")
                break
        except KeyboardInterrupt:
            self.interrupted = True
            self.ledger("INTERRUPTED", "operator stopped the mission")
        finally:
            self.stopping.set()
            try:
                _terminate_active_processes()
            except ConductorError as exc:
                self.fatal_error = self.fatal_error or exc
                self.ledger("PROCESS CLEANUP ERROR", str(exc)[:300])
            for worker_thread in worker_threads:
                worker_thread.join(timeout=30)
                if worker_thread.is_alive():
                    self.fatal_error = self.fatal_error or ConductorError(
                        f"worker did not stop: {worker_thread.name}"
                    )
                    self.ledger(
                        "WORKER CLEANUP ERROR",
                        f"{worker_thread.name} remained alive",
                    )
            if not self.interrupted:
                try:
                    self.distill_lessons()
                except Exception as e:                      # lessons are best-effort
                    self.ledger("LESSONS ERROR", str(e)[:200])
                try:
                    self.retrospective()
                except Exception as e:                      # retro is best-effort
                    self.ledger("RETRO ERROR", str(e)[:200])
            self.current = "mission ended"
            self.write_state()
            self.vault_backup()                    # everything, incl. task branches kept for forensics
            p = self.report(final=True)
            self.ledger("MISSION END", rel_to_root(p))
        return (
            0
            if (
                self.fatal_error is None
                and all(
                    t.status == "done"
                    for t in self.m.tasks
                    if not t.background
                )
            )
            else 1
        )


def main() -> int:
    ap = argparse.ArgumentParser(prog="conductor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run", help="run a mission")
    runp.add_argument("mission", type=Path)
    runp.add_argument("--engine", choices=["claude", "opencode", "kilo"], default=None)
    runp.add_argument("--hours", type=float, default=None)
    retp = sub.add_parser("retro", help="run a standalone process retrospective now")
    retp.add_argument("--engine", choices=["claude", "opencode", "kilo"], default="claude")
    sub.add_parser("status", help="print current STATE.md")
    args = ap.parse_args()

    if args.cmd == "status":
        state = MEMORY / "STATE.md"
        print(state.read_text(encoding="utf-8", errors="replace") if state.exists() else "no mission state yet")
        return 0
    acquire_singleton()
    if args.cmd == "retro":
        # Analyze the most recent mission's telemetry without running anything new.
        path = MEMORY / "PROCESS.jsonl"
        last = "retro"
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    last = json.loads(line).get("mission", last)
                except ValueError:
                    pass
        shell = Mission(name=last, goal="(standalone retrospective)", repo=ROOT,
                        tasks=[], engine=args.engine, hours=1, auto_plan=True)
        Conductor(shell, resume=False).retrospective()
        return 0
    mission = Mission.load(args.mission, args.engine, args.hours)
    return Conductor(mission).run()


if __name__ == "__main__":
    sys.exit(main())
