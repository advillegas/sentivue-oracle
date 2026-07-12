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
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY, REPORTS, LOGS = ROOT / "memory", ROOT / "reports", ROOT / "logs"
WORKTREES = ROOT / ".worktrees"
SWAP_HEALTH = "http://127.0.0.1:9099/health"

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
        time.sleep(1)
    if killed:
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    th.join(timeout=5)
    out = "".join(chunks) + (f"\n{killed}" if killed else "")
    return subprocess.CompletedProcess(args, -9 if killed else p.returncode, out, "")


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
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True)
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_singleton() -> None:
    """One conductor per machine (seed brain O15/L3). Two missions sharing the
    big-model slot starve each other into watchdog kills - observed 2026-07-11:
    parallel 6h and 8h instances produced 18 false-positive kills and zero
    merged work. The lock is advisory-but-checked: stale locks (dead pid)
    are cleaned automatically."""
    lock = ROOT / "state" / "conductor.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8", errors="replace").strip())
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            sys.exit(
                f"REFUSED: another conductor (pid {pid}) is already running a mission.\n"
                "One loop instance per machine (seed brain O15/L3): parallel missions\n"
                "starve each other on the model slot. Stop the other mission first,\n"
                "or delete state/conductor.lock if you are certain it is stale."
            )
    lock.write_text(str(os.getpid()), encoding="utf-8")

    def _release() -> None:
        try:
            if lock.exists() and lock.read_text(encoding="utf-8",
                                                errors="replace").strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass
    atexit.register(_release)


# ---------------------------------------------------------------- mission spec

TASK_FIELDS = {"id", "title", "prompt", "depends_on", "acceptance", "checks", "tier",
               "audit_tier", "timeout_minutes", "stall_minutes", "max_attempts",
               "adversary", "escalate", "background", "requires_approval", "research",
               "best_of_n"}


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
    requires_approval: bool = False  # waits until memory/APPROVALS.md has 'APPROVE <id>'
    research: bool = False           # read-only researcher pass feeds the first attempt
    best_of_n: int = 1               # frontier resampling: N independent candidates on
                                     # attempt 1; checks+audit pick the winner (max 3)
    # runtime state
    status: str = "pending"          # pending|claimed|running|auditing|done|failed|blocked|merge-conflict
    attempts: int = 0
    note: str = ""

    @staticmethod
    def from_dict(d: dict, background: bool = False) -> "Task":
        clean = {k: v for k, v in d.items() if k in TASK_FIELDS}
        clean["background"] = background or bool(clean.get("background"))
        t = Task(**clean)
        t.timeout_minutes = min(int(t.timeout_minutes), 120)
        t.best_of_n = max(1, min(int(t.best_of_n), 3))
        if t.tier not in TIER_MODEL:
            t.tier = "sonnet"
        if t.audit_tier not in TIER_MODEL:
            t.audit_tier = "sonnet"
        return t


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
        raw = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        m = raw.get("mission", {})
        tasks = [Task.from_dict(t) for t in raw.get("tasks", [])]
        tasks += [Task.from_dict(t, background=True) for t in raw.get("background", [])]
        ids = [t.id for t in tasks]
        assert len(ids) == len(set(ids)), "duplicate task ids"
        for t in tasks:
            for d in t.depends_on:
                assert d in ids, f"task {t.id} depends on unknown task {d}"
        repo = Path(os.path.expanduser(m.get("repo", str(ROOT)))).resolve()
        mission = Mission(
            name=m.get("name", path.stem),
            goal=m.get("goal", ""),
            repo=repo,
            tasks=tasks,
            engine=engine or m.get("engine", "claude"),
            hours=hours or float(m.get("hours", 24)),
            report_minutes=int(m.get("report_minutes", 60)),
            auto_plan=bool(m.get("auto_plan", False)),
            workers=max(1, min(int(m.get("workers", 1)), 2)),
        )
        assert mission.tasks or mission.auto_plan, "mission needs tasks or auto_plan=true"
        return mission


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
        if not isinstance(raw, list) or not raw:
            continue
        tasks = []
        for d in raw[:10]:
            if isinstance(d, dict) and d.get("id") and d.get("prompt"):
                d.setdefault("title", d["id"])
                tasks.append(Task.from_dict(d))
        ids = {t.id for t in tasks}
        if len(ids) != len(tasks):
            continue
        for t in tasks:
            t.depends_on = [d for d in t.depends_on if d in ids and d != t.id]
        return tasks
    return []


# ---------------------------------------------------------------- conductor

class Conductor:
    def __init__(self, mission: Mission):
        self.m = mission
        self.t0 = time.monotonic()
        self.deadline = self.t0 + mission.hours * 3600
        self.lock = threading.Lock()        # task state + memory files
        self.gitlock = threading.Lock()     # worktree add / merge / branch ops
        self.biglock = threading.Lock()     # big-slot serialization (prevents swap thrash)
        self.sweeplock = threading.Lock()   # one regression sweep at a time
        self.interrupted = False
        self.current: str = "starting up"
        self.stopping = threading.Event()
        self.replanned = False
        for d in (MEMORY, REPORTS, LOGS, WORKTREES):
            d.mkdir(parents=True, exist_ok=True)
        self.use_git = git(self.m.repo, "rev-parse", "--git-dir").returncode == 0
        # NB: task branches live under task/<mission>/ (not under the mission branch
        # name) because git forbids a branch that is a path-prefix of another.
        self.branch = f"mission/{self.m.name}"

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
        rec = {"ts": now(), "mission": self.m.name, "kind": kind, **kw}
        with self.lock:
            with (MEMORY / "PROCESS.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

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
            (MEMORY / "STATE.md").write_text(state, encoding="utf-8")

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
    def make_worktree(self, t: Task) -> Path:
        """One worktree per task, reused across retry attempts so the developer
        iterates on prior work instead of starting over."""
        if not self.use_git:
            return self.m.repo
        with self.gitlock:
            if git(self.m.repo, "rev-parse", "--verify", self.branch).returncode != 0:
                git(self.m.repo, "branch", self.branch)
            wt = WORKTREES / f"{self.m.name}-{t.id}"
            if wt.exists():
                return wt
            git(self.m.repo, "worktree", "prune")
            git(self.m.repo, "branch", "-D", self.task_branch(t))   # stale from a prior run
            r = git(self.m.repo, "worktree", "add", "-b", self.task_branch(t), str(wt), self.branch)
            if r.returncode != 0:
                self.ledger("WORKTREE ERROR", r.stdout[-400:])
                return self.m.repo
            return wt

    def merge_task(self, t: Task, wt: Path, branch_name: str | None = None) -> bool:
        """Advance the mission branch to the audited task tip.

        Sequential merges from the current mission tip make this a fast-forward,
        implemented as a ref move (`branch -f`) so it never touches whatever the
        operator has checked out. With parallel workers a rebase brings the task
        branch up to date first."""
        if not self.use_git or wt == self.m.repo:
            return True
        with self.gitlock:
            git(wt, "add", "-A")
            git(wt, "commit", "-m", f"task({t.id}): {t.title} [audit: pass]")
            task_branch = branch_name or self.task_branch(t)
            if git(self.m.repo, "merge-base", "--is-ancestor", self.branch, task_branch).returncode != 0:
                r = git(wt, "rebase", self.branch)
                if r.returncode != 0:
                    git(wt, "rebase", "--abort")
                    t.status = "merge-conflict"
                    self.ledger("MERGE CONFLICT", f"{t.id}: rebase onto {self.branch} failed; "
                                                  f"work kept in {wt}")
                    return False
            r = git(self.m.repo, "branch", "-f", self.branch, task_branch)
            if r.returncode != 0:
                t.status = "merge-conflict"
                self.ledger("MERGE ERROR", f"{t.id}: could not advance {self.branch} "
                                           f"({r.stdout.strip()[-300:]}); work kept in {wt}")
                return False
            git(self.m.repo, "worktree", "remove", "--force", str(wt))
            git(self.m.repo, "branch", "-D", task_branch)
        self.vault_backup([self.branch])          # offline private remote, if configured
        return True

    def make_candidate_worktree(self, t: Task, k: int) -> tuple[Path | None, str]:
        """Independent worktree+branch for tournament candidate k (fresh from the
        mission tip - candidates never see each other's work)."""
        if not self.use_git:
            return None, ""
        with self.gitlock:
            if git(self.m.repo, "rev-parse", "--verify", self.branch).returncode != 0:
                git(self.m.repo, "branch", self.branch)
            branch = f"{self.task_branch(t)}-cand{k}"
            wt = WORKTREES / f"{self.m.name}-{t.id}-cand{k}"
            if wt.exists():
                git(self.m.repo, "worktree", "remove", "--force", str(wt))
            git(self.m.repo, "worktree", "prune")
            git(self.m.repo, "branch", "-D", branch)
            r = git(self.m.repo, "worktree", "add", "-b", branch, str(wt), self.branch)
            if r.returncode != 0:
                self.ledger("WORKTREE ERROR", f"cand{k}: {r.stdout[-300:]}")
                return None, ""
            return wt, branch

    def drop_worktree(self, wt: Path | None, branch: str) -> None:
        if not self.use_git or wt is None:
            return
        with self.gitlock:
            git(self.m.repo, "worktree", "remove", "--force", str(wt))
            if branch:
                git(self.m.repo, "branch", "-D", branch)

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
        (LOGS / f"{self.m.name}-{tag}.log").write_text(r.stdout, encoding="utf-8")
        text = extract_result(self.m.engine, r.stdout)
        if "[WATCHDOG]" in r.stdout:
            text += "\n[WATCHDOG] killed"
        return text

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
        out = self.run_engine(prompt, tier, self.m.repo, 30, "plan", stall_min=12)
        tasks = parse_plan(out)
        if not tasks:
            self.ledger("PLANNING FAILED", "no valid JSON plan produced")
            return False
        background = [t for t in self.m.tasks if t.background]
        self.m.tasks = tasks + background
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
        out = self.run_engine(prompt, tier, self.m.repo, 30, "replan", stall_min=12)
        new = parse_plan(out)
        if not new:
            self.ledger("REPLAN FAILED", "no valid JSON plan produced")
            return False
        keep = [t for t in self.m.tasks if t.status == "done" or t.background]
        valid_ids = {t.id for t in keep} | {t.id for t in new}
        for t in new:
            if any(k.id == t.id for k in keep):
                t.id = f"{t.id}-r2"
            t.depends_on = [d for d in t.depends_on if d in valid_ids]
        with self.lock:
            self.m.tasks = keep + new
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
        out = self.run_engine(prompt, "haiku", wt, 12, f"{t.id}-research", stall_min=8).strip()
        if out:
            (wt / "RESEARCH.md").write_text(f"# Researcher brief â€” {t.id}\n\n{out}\n",
                                            encoding="utf-8")
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
            return True, ""
        fails = []
        for c in t.checks:
            r = sh(shell_check(c), cwd=wt, timeout=900)
            status = "OK" if r.returncode == 0 else f"EXIT {r.returncode}"
            self.ledger(f"CHECK {t.id}", f"[{status}] $ {c}")
            if r.returncode != 0:
                fails.append(f"$ {c}\n{r.stdout[-1500:]}")
        return (not fails), "\n\n".join(fails)

    def audit(self, t: Task, wt: Path, tier: str | None = None, tag: str = "",
              concerns: str = "") -> tuple[bool, str]:
        t.status = "auditing"
        self.write_state()
        diff = ""
        if self.use_git:
            git(wt, "add", "-A")
            git(wt, "commit", "-m", f"wip({t.id}): pre-audit snapshot")
            diff = git(wt, "diff", f"{self.branch}...HEAD", "--stat").stdout[-3000:]
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
        out = self.run_engine(prompt, tier or t.audit_tier, wt, 25,
                              f"{t.id}-audit{t.attempts}{tag}", stall_min=12)
        m = re.findall(r"AUDIT:\s*(PASS|FAIL:?.*)", out)
        verdict = m[-1] if m else "FAIL: auditor produced no verdict"
        return verdict.startswith("PASS"), verdict

    def adversary_pass(self, t: Task, wt: Path) -> None:
        prompt = (
            f"You are the ADVERSARY. Attack the work for task [{t.id}] '{t.title}' in the "
            "current directory: edge cases, statistical validity, silent failures, wasted effort. "
            "Reproduce anything you claim. End with 'ADVERSARY: <c> critical, <m> major, <n> minor'."
        )
        out = self.run_engine(prompt, "opus", wt, 30, f"{t.id}-adversary", stall_min=15)
        m = re.findall(r"ADVERSARY:.*", out)
        self.ledger(f"ADVERSARY on {t.id}", m[-1] if m else "no summary line")

    # ---- task lifecycle -----------------------------------------------------
    def cutoff(self) -> float:
        """Stop dispatching new engine runs 10 minutes before the deadline."""
        return self.deadline - 600

    def best_of_n_round(self, t: Task, brief: str) -> bool:
        """Frontier resampling (seed brain: 'best-of-n plus adversarial
        verification where correctness outranks latency - free with local
        tokens'). Generate N independent candidates from the same base, gate
        each through deterministic checks, audit the survivors, merge the first
        candidate that passes everything. Returns True when a winner merged."""
        n = t.best_of_n
        self.ledger(f"TOURNAMENT {t.id}", f"best-of-{n}: independent candidates, "
                                          "checks gate, audit picks the winner")
        survivors: list[tuple[int, Path, str]] = []
        for k in range(1, n + 1):
            if time.monotonic() >= self.cutoff():
                break
            wt, branch = self.make_candidate_worktree(t, k)
            if wt is None:
                return False                      # no git: caller falls back to single-path
            if brief:
                (wt / "RESEARCH.md").write_text(f"# Researcher brief — {t.id}\n\n{brief}\n",
                                                encoding="utf-8")
            out = self.run_engine(self.developer_prompt(t, "", brief), t.tier, wt,
                                  t.timeout_minutes, f"{t.id}-cand{k}",
                                  stall_min=t.stall_minutes)
            if "[WATCHDOG] killed" in out or re.match(r"API Error:", out.strip()):
                self.ledger(f"TOURNAMENT {t.id}", f"candidate {k}: run failed (infra/watchdog)")
                self.drop_worktree(wt, branch)
                continue
            ok, checks_out = self.run_checks(t, wt)
            self.proc("candidate", task=t.id, k=k, checks="pass" if ok else "fail")
            if ok:
                survivors.append((k, wt, branch))
            else:
                self.ledger(f"TOURNAMENT {t.id}", f"candidate {k}: checks failed")
                self.drop_worktree(wt, branch)
        self.ledger(f"TOURNAMENT {t.id}", f"{len(survivors)}/{n} candidates passed checks")
        winner_found = False
        for k, wt, branch in survivors:
            if not winner_found:
                ok, verdict = self.audit(t, wt)
                self.ledger(f"AUDIT {t.id}", f"candidate {k}: {verdict[:200]}")
                if ok:
                    if t.adversary:
                        self.adversary_pass(t, wt)
                    if self.merge_task(t, wt, branch_name=branch):
                        t.status = "done"
                        self.proc("attempt", task=t.id, attempt=t.attempts, tier=t.tier,
                                  escalated=False, outcome="done", tournament=k)
                        self.regression_sweep()
                        winner_found = True
                        continue
            self.drop_worktree(wt, branch)
        return winner_found

    def run_task(self, t: Task) -> None:
        feedback = ""
        infra_strikes = 0
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
            if self.best_of_n_round(t, brief):
                return
            feedback = ("A best-of-N tournament ran: no candidate passed checks+audit. "
                        "Study FEEDBACK/RESEARCH context and take a fundamentally "
                        "different approach.")
            self.log_failure(t, "tournament", f"best-of-{t.best_of_n}: no candidate survived")
        while t.attempts < t.max_attempts:
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
            def attempt_end(outcome: str, **kw) -> None:
                self.proc("attempt", task=t.id, attempt=t.attempts, tier=tier,
                          escalated=tier != t.tier, outcome=outcome,
                          secs=round(time.monotonic() - attempt_t0), **kw)

            # Engine/API failures are INFRASTRUCTURE, not task failures: the model
            # never got to work, so the attempt must not count against the task.
            # Heal the serving layer, back off, and retry (bounded).
            stripped = out.strip()
            if (re.match(r"API Error:", stripped)
                    or "exceeds the available context size" in stripped
                    or (len(stripped) < 200 and re.search(r"\b(ECONNREFUSED|fetch failed)\b", stripped))):
                infra_strikes += 1
                t.attempts -= 1                       # refund the attempt
                self.ledger(f"INFRA {t.id}",
                            f"engine/API failure (strike {infra_strikes}/3): {stripped[:200]}")
                self.log_failure(t, "infra", stripped[:400])
                self.proc("infra", task=t.id, strike=infra_strikes, detail=stripped[:200])
                if infra_strikes >= 3:
                    t.status = "blocked"
                    t.note = f"infrastructure failure x{infra_strikes}: {stripped[:200]}"
                    self.ledger(f"BLOCKED {t.id}", "persistent engine/API failure â€” "
                                "check serving context size and engine config")
                    return
                self.ensure_serving()
                time.sleep(30)
                continue
            if "[WATCHDOG] killed" in out:
                feedback = ("The previous attempt hung (or went silent) and was killed. "
                            "The approach was likely too monolithic â€” decompose the work, "
                            "commit after every step, avoid long-running commands.")
                self.ledger(f"WATCHDOG {t.id}", "stalled run killed â€” retrying")
                self.log_failure(t, "watchdog-kill",
                                 "run exceeded its time box or went silent and was killed")
                attempt_end("watchdog")
                continue
            last_line = out.strip().splitlines()[-1] if out.strip() else ""
            if re.search(r"^BLOCKED:", last_line, re.M):
                t.status, t.note = "blocked", last_line[:300]
                self.ledger(f"BLOCKED {t.id}", t.note)
                self.log_failure(t, "blocked", t.note)
                attempt_end("blocked")
                return
            if re.search(r"^NEEDS_CONTEXT:", last_line, re.M):
                # A6/A5: under-specified task. Retrying the same prompt harder is
                # waste â€” surface it for the planner/operator instead.
                t.status, t.note = "blocked", last_line[:300]
                self.ledger(f"NEEDS_CONTEXT {t.id}", t.note)
                self.log_failure(t, "needs-context", t.note)
                attempt_end("needs-context")
                return
            concerns = ""
            m_conc = re.search(r"^DONE_WITH_CONCERNS:?\s*(.*)$", last_line, re.M)
            if m_conc:
                concerns = m_conc.group(1).strip()[:400]
                self.ledger(f"CONCERNS {t.id}", concerns or "(unspecified)")
            checks_ok, checks_out = self.run_checks(t, wt)
            if not checks_ok:
                self.log_failure(t, "checks-fail", checks_out[:600])
                feedback = f"Deterministic checks failed:\n{checks_out}"
                attempt_end("checks-fail")
                continue
            ok, verdict = self.audit(t, wt, concerns=concerns)
            tiebreak = False
            if (not ok and t.checks
                    and TIER_MODEL["opus"] != TIER_MODEL[t.audit_tier]):
                # Checks passed but the auditor disagrees â€” tiebreak with the
                # strongest model (skipped when the profile maps opus to the same
                # model, where a re-audit would add nothing).
                ok2, verdict2 = self.audit(t, wt, tier="opus", tag="-tiebreak")
                if ok2:
                    ok, verdict = True, verdict2 + " [opus tiebreak overrode initial FAIL]"
                    tiebreak = True
            self.ledger(f"AUDIT {t.id}", verdict[:300])
            if ok:
                if t.adversary:
                    self.adversary_pass(t, wt)
                if self.merge_task(t, wt):
                    t.status = "done"
                    attempt_end("done", tiebreak=tiebreak)
                    self.regression_sweep()
                else:
                    attempt_end("merge-conflict")
                return
            self.log_failure(t, "audit-fail", verdict)
            attempt_end("audit-fail")
            feedback = verdict
        t.status, t.note = "failed", f"failed audit {t.max_attempts} times: {feedback[:200]}"
        self.ledger(f"FAILED {t.id}", t.note)
        self.log_failure(t, "exhausted", t.note)

    # ---- approvals (guardian countersign for irreversible work) ---------------
    def approved(self, t: Task) -> bool:
        """Irreversible tasks wait for the operator to write 'APPROVE <id>' into
        memory/APPROVALS.md â€” a guardian countersign, not a rubber stamp."""
        if not t.requires_approval:
            return True
        ap = MEMORY / "APPROVALS.md"
        if not ap.exists():
            ap.write_text("# Operator approvals â€” write 'APPROVE <task-id>' on its own "
                          "line to release an irreversible task.\n\n", encoding="utf-8")
            return False
        return re.search(rf"^APPROVE\s+{re.escape(t.id)}\s*$",
                         ap.read_text(encoding="utf-8", errors="replace"), re.M) is not None

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
        NB: ledger() takes self.lock too, so ledger events are emitted only after
        the lock is released (self.lock is not reentrant)."""
        approval_events: list[tuple[str, str]] = []
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
                if not self.approved(t):
                    if "awaiting operator approval" not in t.note:
                        t.note = ("awaiting operator approval (memory/APPROVALS.md: "
                                  f"'APPROVE {t.id}')")
                        approval_events.append((t.id, t.note))
                    continue
                t.status = "claimed"
                picked = t
                break
        for tid, note in approval_events:
            self.ledger(f"APPROVAL NEEDED {tid}", note)
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
        if not self.sweeplock.acquire(blocking=False):
            return                       # a sweep is already running; next merge re-sweeps
        try:
            wt = WORKTREES / f"{self.m.name}-gates"
            with self.gitlock:
                if not wt.exists():
                    r = git(self.m.repo, "worktree", "add", "--detach", str(wt), self.branch)
                    if r.returncode != 0:
                        self.ledger("GATES ERROR", r.stdout[-300:])
                        return
                else:
                    git(wt, "checkout", "--force", "--detach", self.branch)
            reopened = []
            for x in prior:
                for c in x.checks:
                    r = sh(shell_check(c), cwd=wt, timeout=900)
                    if r.returncode != 0:
                        with self.lock:
                            x.status = "pending"
                            x.attempts = min(x.attempts, x.max_attempts - 1)
                            x.note = f"REGRESSION: gate broke after a later merge: {c}"
                        self.log_failure(x, "regression", f"$ {c}\n{r.stdout[-500:]}")
                        self.proc("regression", task=x.id, check=c)
                        reopened.append(x.id)
                        break
            if reopened:
                self.ledger("REGRESSION", "reopened: " + ", ".join(reopened))
            else:
                self.ledger("GATES", f"regression sweep clean ({len(prior)} prior tasks)")
        finally:
            self.sweeplock.release()

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
            self.run_task(t)

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
        out = self.run_engine(prompt, "haiku", self.m.repo, 10, "lessons", stall_min=8).strip()
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
        out = self.run_engine(prompt, tier, ROOT, 25, f"retro-{date}", stall_min=15)
        (REPORTS / f"RETRO-{dt.datetime.now():%Y%m%d-%H%M}.md").write_text(
            f"# Retrospective â€” mission `{self.m.name}`\n\n{now()}\n\n{out}\n", encoding="utf-8")

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
            "#   echo APPROVE <task-id> >> memory/APPROVALS.md",
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
        out = self.run_engine(prompt, "haiku", self.m.repo, 8,
                              f"overseer-{dt.datetime.now():%H%M}", stall_min=6).strip()
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
            lines += ["\n**AWAITING OPERATOR APPROVAL** (write `APPROVE <id>` into "
                      "`memory/APPROVALS.md`): " + ", ".join(t.id for t in waiting)]
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
        threading.Thread(target=self._report_thread, daemon=True).start()
        if self.m.workers > 1:
            threading.Thread(target=self.fast_worker, daemon=True).start()
        try:
            while time.monotonic() < self.cutoff():
                self.write_state()
                t = self.claim(False) or self.claim(True)
                if t:
                    self.run_task(t)
                    continue
                if self.anything_active():        # parallel worker is mid-task
                    time.sleep(15)
                    continue
                if self.all_terminal():
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
        return 0 if all(t.status == "done" for t in self.m.tasks if not t.background) else 1


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
        Conductor(shell).retrospective()
        return 0
    acquire_singleton()
    mission = Mission.load(args.mission, args.engine, args.hours)
    return Conductor(mission).run()


if __name__ == "__main__":
    sys.exit(main())
