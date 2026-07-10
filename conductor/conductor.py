#!/usr/bin/env python3
"""
SentiVue Oracle conductor — the self-governing mission loop.

Contract (see README):
  Automations   mission TOML: goal, tasks, dependencies, acceptance criteria — or a
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
    for _line in _tiers_file.read_text(encoding="utf-8").splitlines():
        _k, _, _v = _line.partition("=")
        _tier = _k.strip().removesuffix("_MODEL").lower()
        if _tier in TIER_MODEL and _v.strip():
            TIER_MODEL[_tier] = _v.strip()


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sh(args: list[str], cwd: Path | None = None, timeout: int | None = None,
       env: dict | None = None, stall_timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command in its own session with a total timeout AND an optional
    output-stall timeout (kill when the process goes silent for too long —
    catches hung runs long before the total budget is burned)."""
    full_env = {**os.environ, **(env or {})}
    p = subprocess.Popen(args, cwd=cwd, env=full_env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", start_new_session=True)
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
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
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


# ---------------------------------------------------------------- mission spec

TASK_FIELDS = {"id", "title", "prompt", "depends_on", "acceptance", "checks", "tier",
               "audit_tier", "timeout_minutes", "stall_minutes", "max_attempts",
               "adversary", "escalate", "background", "requires_approval"}


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
    stall_minutes: int = 12          # output-silence kill (swap+prefill can take minutes)
    max_attempts: int = 3
    adversary: bool = False          # extra adversarial pass after audit
    escalate: bool = True            # final attempt auto-escalates to opus tier
    background: bool = False         # only runs when nothing else is dispatchable
    requires_approval: bool = False  # waits until memory/APPROVALS.md has 'APPROVE <id>'
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
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
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

def engine_cmd(engine: str, prompt: str, tier: str) -> tuple[list[str], dict]:
    """argv + extra env for one headless engine run (full autonomy: dedicated box).
    Claude Code uses stream-json so the stall detector sees per-event activity."""
    if engine == "claude":
        return (["bash", str(ROOT / "engines/claude-code/launch.sh"), "-p", prompt,
                 "--model", tier, "--output-format", "stream-json", "--verbose",
                 "--dangerously-skip-permissions"], {})
    if engine == "opencode":
        return (["bash", str(ROOT / "engines/opencode/launch.sh"), "run",
                 "-m", f"oracle/{TIER_MODEL[tier]}", prompt],
                {"OPENCODE_PERMISSION": json.dumps(
                    {"edit": "allow", "bash": "allow", "webfetch": "deny"})})
    raise ValueError(f"unknown engine {engine!r} (use claude|opencode)")


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


PLAN_CONTRACT = """Respond with STRICT JSON inside a single ```json fenced block: an array of
3-10 task objects, each:
{"id": "<kebab-slug>", "title": "<short>", "prompt": "<detailed, fully self-contained
instructions for an engineer with NO memory of this conversation>",
 "depends_on": ["<ids>"], "acceptance": ["<criterion>", ...],
 "checks": ["<shell command that exits 0 iff the criterion holds>", ...],
 "tier": "sonnet"|"haiku"|"opus", "timeout_minutes": <int, <=90>, "adversary": <bool>,
 "requires_approval": <bool>}
Rules: ids unique; depends_on must form a DAG over these ids; every task must be
independently verifiable; put the mechanical proof of every acceptance criterion into
checks wherever a shell command can express it; use tier "haiku" for grunt work,
"opus" only where deep reasoning is essential; adversary=true for risk-bearing tasks.
Destructive or irreversible operations (database mutations, mass deletes/merges,
schema changes, anything hard to undo) MUST be split into two tasks: a read-only
DRY-RUN task that produces a reviewable report, and an EXECUTE task that depends on
it and carries requires_approval=true. Long-running jobs (big backfills, full
walk-forwards) MUST be split into a launch task that starts a RESUMABLE,
checkpointed background job and a separate later verification task — never hold a
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
        line = f"- **{now()}** [{self.m.name}] {event}" + (f" — {detail}" if detail else "")
        path = MEMORY / "LEDGER.md"
        with self.lock:
            if not path.exists():
                path.write_text("# SentiVue Oracle — Ledger (append-only)\n\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        print(line, flush=True)

    def log_failure(self, t: Task, kind: str, detail: str) -> None:
        """Failure memory: future runs grep this before attempting anything —
        the cheapest way to stop a 24 h mission from re-running dead ends."""
        p = MEMORY / "FAILURES.md"
        with self.lock:
            if not p.exists():
                p.write_text("# Failure memory — what did not work and why. "
                             "Search this before any risky attempt.\n\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as f:
                f.write(f"## {now()} · {self.m.name}/{t.id} · attempt {t.attempts} · {kind}\n"
                        f"{detail.strip()[:600]}\n\n")

    def write_state(self) -> None:
        rows = "\n".join(
            f"| {t.id} | {t.status} | {t.attempts}/{t.max_attempts} | {t.tier} | {t.title} |"
            for t in self.m.tasks)
        left = max(0.0, (self.deadline - time.monotonic()) / 3600)
        state = (
            f"# STATE — mission `{self.m.name}` ({self.m.engine} engine)\n\n"
            f"Updated: {now()} · Time left: {left:.1f} h\nGoal: {self.m.goal}\n\n"
            f"Now: {self.current}\n\n"
            f"| task | status | attempts | tier | title |\n|---|---|---|---|---|\n{rows}\n"
        )
        with self.lock:
            (MEMORY / "STATE.md").write_text(state, encoding="utf-8")

    # ---- self-healing -------------------------------------------------------
    def ensure_serving(self) -> None:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(SWAP_HEALTH, timeout=5):
                    return
            except Exception:
                self.ledger("SELF-HEAL", f"llama-swap unhealthy — restart attempt {attempt + 1}")
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

    def merge_task(self, t: Task, wt: Path) -> bool:
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
            task_branch = self.task_branch(t)
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
            return True

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
        plan_md = "\n".join(f"- **{t.id}** ({t.tier}, deps: {t.depends_on or '—'}): {t.title}"
                            for t in tasks)
        (MEMORY / "MISSION-PLAN.md").write_text(
            f"# Mission plan — {self.m.name}\n\n{now()}\nGoal: {self.m.goal}\n\n{plan_md}\n",
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
            failures = fpath.read_text(encoding="utf-8")[-3000:]
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

    # ---- prompts ---------------------------------------------------------------
    def developer_prompt(self, t: Task, feedback: str) -> str:
        acc = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(t.acceptance)) or "  (none listed)"
        gates = ""
        if t.checks:
            gates = ("\n\nMECHANICAL GATES — the conductor runs these itself after you finish; "
                     "every one must exit 0:\n" +
                     "\n".join(f"  $ {c}" for c in t.checks))
        fb = ""
        if feedback:
            fb = (f"\n\nATTEMPT {t.attempts} — THE PREVIOUS ATTEMPT FAILED. Full details are in "
                  "FEEDBACK.md in this directory; the summary:\n"
                  f"{feedback[:1200]}\n"
                  "Do NOT re-run the failed approach harder. First write the DIAGNOSIS block in "
                  "TASKPLAN.md (root cause, not symptom — 5 lines), then execute a changed plan. "
                  "Repeating a logged failure wastes the mission's budget.")
        return (
            f"MISSION: {self.m.goal}\nTASK [{t.id}]: {t.title}\n\n{t.prompt}\n\n"
            f"ACCEPTANCE CRITERIA (audited independently — all must demonstrably hold):\n"
            f"{acc}{gates}{fb}\n\n"
            "You operate under the Long-Horizon Autonomy Protocol (in your loaded instructions). "
            "Non-negotiables for this run:\n"
            f"1. START RITUAL before any edit: read {MEMORY / 'STATE.md'}, the tail of "
            f"{MEMORY / 'LEDGER.md'}, {MEMORY / 'LESSONS.md'} (hard-won knowledge — do not "
            f"relearn it), and {MEMORY / 'FAILURES.md'} (search '{t.id}'); "
            "run `git log --oneline -10` and `git status` here; read TASKPLAN.md if present.\n"
            "2. Write TASKPLAN.md (GOAL / 3-7 STEPS each with a CHECK / NOT-DOING) before "
            "touching code; keep it updated — it is your anchor against drift.\n"
            "3. Work the ratchet: one step, run its CHECK, commit "
            "(`bash $ORACLE_ROOT/bin/checkpoint \"msg\"` does commit + ledger in one step), "
            "next step. Never end a step with a broken tree.\n"
            "4. Evidence standard: a criterion counts only with the command AND its fresh output.\n"
            "5. Re-anchor every ~10 actions: re-read the criteria and your current step.\n"
            "5b. Destructive or hard-to-undo operations (DB mutations, mass deletes/renames): "
            "dry-run first, show the dry-run report, only then execute. Probes are read-only.\n"
            f"6. Finish: full test suite from clean state, commit, ledger entry to "
            f"{MEMORY / 'LEDGER.md'} (what/why/files/next).\n"
            "If two genuinely different strategies fail, end with 'BLOCKED: <what you know, "
            "what you ruled out, what you would try next>'."
        )

    # ---- verification stack ------------------------------------------------------
    def run_checks(self, t: Task, wt: Path) -> tuple[bool, str]:
        """Deterministic gates. LLM judgment only begins after exit codes agree."""
        if not t.checks:
            return True, ""
        fails = []
        for c in t.checks:
            r = sh(["bash", "-lc", c], cwd=wt, timeout=900)
            status = "OK" if r.returncode == 0 else f"EXIT {r.returncode}"
            self.ledger(f"CHECK {t.id}", f"[{status}] $ {c}")
            if r.returncode != 0:
                fails.append(f"$ {c}\n{r.stdout[-1500:]}")
        return (not fails), "\n\n".join(fails)

    def audit(self, t: Task, wt: Path, tier: str | None = None, tag: str = "") -> tuple[bool, str]:
        t.status = "auditing"
        self.write_state()
        diff = ""
        if self.use_git:
            git(wt, "add", "-A")
            git(wt, "commit", "-m", f"wip({t.id}): pre-audit snapshot")
            diff = git(wt, "diff", f"{self.branch}...HEAD", "--stat").stdout[-3000:]
        acc = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(t.acceptance)) or "  (none listed)"
        prompt = (
            f"You are the AUDITOR. Verify task [{t.id}] '{t.title}' in the current directory.\n"
            f"ACCEPTANCE CRITERIA:\n{acc}\n\nCHANGE SUMMARY:\n{diff}\n\n"
            "Run the tests yourself. Inspect the actual changes. Do NOT fix anything.\n"
            "Also flag scope drift (changed files the task did not require) and any test that "
            "was weakened or deleted to force a pass — both are FAIL reasons.\n"
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

    def run_task(self, t: Task) -> None:
        feedback = ""
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
            wt = self.make_worktree(t)
            if feedback:
                (wt / "FEEDBACK.md").write_text(
                    f"# Attempt {t.attempts - 1} failure details\n\n{feedback}\n", encoding="utf-8")
            # Thinking models emit no stream events mid-thought; give opus runs
            # a longer silence allowance before the stall watchdog fires.
            stall = max(t.stall_minutes, 20) if tier == "opus" else t.stall_minutes
            out = self.run_engine(self.developer_prompt(t, feedback), tier, wt,
                                  t.timeout_minutes, f"{t.id}-dev{t.attempts}",
                                  stall_min=stall)
            if "[WATCHDOG] killed" in out:
                feedback = ("The previous attempt hung (or went silent) and was killed. "
                            "The approach was likely too monolithic — decompose the work, "
                            "commit after every step, avoid long-running commands.")
                self.ledger(f"WATCHDOG {t.id}", "stalled run killed — retrying")
                self.log_failure(t, "watchdog-kill",
                                 "run exceeded its time box or went silent and was killed")
                continue
            last_line = out.strip().splitlines()[-1] if out.strip() else ""
            if re.search(r"^BLOCKED:", last_line, re.M):
                t.status, t.note = "blocked", last_line[:300]
                self.ledger(f"BLOCKED {t.id}", t.note)
                self.log_failure(t, "blocked", t.note)
                return
            checks_ok, checks_out = self.run_checks(t, wt)
            if not checks_ok:
                self.log_failure(t, "checks-fail", checks_out[:600])
                feedback = f"Deterministic checks failed:\n{checks_out}"
                continue
            ok, verdict = self.audit(t, wt)
            if (not ok and t.checks
                    and TIER_MODEL["opus"] != TIER_MODEL[t.audit_tier]):
                # Checks passed but the auditor disagrees — tiebreak with the
                # strongest model (skipped when the profile maps opus to the same
                # model, where a re-audit would add nothing).
                ok2, verdict2 = self.audit(t, wt, tier="opus", tag="-tiebreak")
                if ok2:
                    ok, verdict = True, verdict2 + " [opus tiebreak overrode initial FAIL]"
            self.ledger(f"AUDIT {t.id}", verdict[:300])
            if ok:
                if t.adversary:
                    self.adversary_pass(t, wt)
                if self.merge_task(t, wt):
                    t.status = "done"
                    self.regression_sweep()
                return
            self.log_failure(t, "audit-fail", verdict)
            feedback = verdict
        t.status, t.note = "failed", f"failed audit {t.max_attempts} times: {feedback[:200]}"
        self.ledger(f"FAILED {t.id}", t.note)
        self.log_failure(t, "exhausted", t.note)

    # ---- approvals (guardian countersign for irreversible work) ---------------
    def approved(self, t: Task) -> bool:
        """Irreversible tasks wait for the operator to write 'APPROVE <id>' into
        memory/APPROVALS.md — a guardian countersign, not a rubber stamp."""
        if not t.requires_approval:
            return True
        ap = MEMORY / "APPROVALS.md"
        if not ap.exists():
            ap.write_text("# Operator approvals — write 'APPROVE <task-id>' on its own "
                          "line to release an irreversible task.\n\n", encoding="utf-8")
            return False
        return re.search(rf"^APPROVE\s+{re.escape(t.id)}\s*$",
                         ap.read_text(encoding="utf-8"), re.M) is not None

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
                    r = sh(["bash", "-lc", c], cwd=wt, timeout=900)
                    if r.returncode != 0:
                        with self.lock:
                            x.status = "pending"
                            x.attempts = min(x.attempts, x.max_attempts - 1)
                            x.note = f"REGRESSION: gate broke after a later merge: {c}"
                        self.log_failure(x, "regression", f"$ {c}\n{r.stdout[-500:]}")
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
        lessons in memory/LESSONS.md — the mechanism that makes mission N+1 start
        smarter than mission N (the 'do not relearn these' file)."""
        if not any(t.attempts for t in self.m.tasks):
            return
        def tail(p: Path, n: int) -> str:
            return p.read_text(encoding="utf-8")[-n:] if p.exists() else ""
        prompt = (
            "You are the mission historian. From the records below, distill 3-8 numbered "
            "LESSONS for future missions in this repository: reusable facts, gotchas, "
            "commands that work, approaches that are proven dead. One line each, "
            "imperative, general (no mission-specific trivia). Respond with ONLY the "
            f"numbered list.\n\nLEDGER (tail):\n{tail(MEMORY / 'LEDGER.md', 6000)}\n\n"
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
                p.write_text("# Lessons — distilled at mission end. Read during the start "
                             "ritual; do not relearn these.\n\n", encoding="utf-8")
            with p.open("a", encoding="utf-8") as f:
                f.write(f"## {now()} · mission {self.m.name}\n" + "\n".join(lines) + "\n\n")
        self.ledger("LESSONS", f"{len(lines)} lessons distilled to memory/LESSONS.md")

    # ---- reporting ----------------------------------------------------------
    def report(self, final: bool = False) -> Path:
        counts: dict[str, int] = {}
        for t in self.m.tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        elapsed = (time.monotonic() - self.t0) / 3600
        name = "FINAL-REPORT.md" if final else f"REPORT-{dt.datetime.now():%Y%m%d-%H%M}.md"
        lines = [
            f"# {'Final report' if final else 'Hourly report'} — mission `{self.m.name}`",
            f"\n{now()} · engine {self.m.engine} · elapsed {elapsed:.1f} h of {self.m.hours} h",
            f"\n**Happening now:** {self.current}",
            f"\n**Status:** " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
            "\n## Tasks\n| id | status | attempts | note |\n|---|---|---|---|",
        ]
        lines += [f"| {t.id} | {t.status} | {t.attempts} | {t.note} |" for t in self.m.tasks]
        waiting = self.awaiting_approval()
        if waiting:
            lines += ["\n**AWAITING OPERATOR APPROVAL** (write `APPROVE <id>` into "
                      "`memory/APPROVALS.md`): " + ", ".join(t.id for t in waiting)]
        nxt = self.dispatchable(False)
        lines += [f"\n**Planned next:** {nxt.id + ' — ' + nxt.title if nxt else 'nothing pending'}"]
        try:
            tail = (MEMORY / "LEDGER.md").read_text(encoding="utf-8").splitlines()[-12:]
            lines += ["\n## Ledger tail", *tail]
        except FileNotFoundError:
            pass
        p = REPORTS / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def _report_thread(self) -> None:
        while not self.stopping.wait(self.m.report_minutes * 60):
            p = self.report()
            self.ledger("HOURLY REPORT", str(p.relative_to(ROOT)))

    # ---- main loop ----------------------------------------------------------
    def run(self) -> int:
        self.ledger("MISSION START",
                    f"goal='{self.m.goal}' engine={self.m.engine} budget={self.m.hours}h "
                    f"tasks={len(self.m.tasks)} workers={self.m.workers} repo={self.m.repo}")
        if not self.use_git:
            self.ledger("WARN", "target repo is not a git repo — running without worktree isolation")
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
                self.current = f"stalled — {len(pending)} tasks blocked by failed dependencies"
                self.ledger("STALLED", self.current + " — ending early rather than idling")
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
            self.current = "mission ended"
            self.write_state()
            p = self.report(final=True)
            self.ledger("MISSION END", str(p.relative_to(ROOT)))
        return 0 if all(t.status == "done" for t in self.m.tasks if not t.background) else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="conductor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run", help="run a mission")
    runp.add_argument("mission", type=Path)
    runp.add_argument("--engine", choices=["claude", "opencode"], default=None)
    runp.add_argument("--hours", type=float, default=None)
    sub.add_parser("status", help="print current STATE.md")
    args = ap.parse_args()

    if args.cmd == "status":
        state = MEMORY / "STATE.md"
        print(state.read_text(encoding="utf-8") if state.exists() else "no mission state yet")
        return 0
    mission = Mission.load(args.mission, args.engine, args.hours)
    return Conductor(mission).run()


if __name__ == "__main__":
    sys.exit(main())
