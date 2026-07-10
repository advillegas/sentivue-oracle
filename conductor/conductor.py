#!/usr/bin/env python3
"""
SentiVue Oracle conductor — the self-governing mission loop.

Contract (see README):
  Automations  mission TOML: goal, tasks, dependencies, acceptance criteria
  Worktrees    every task runs in an isolated git worktree; merge only after audit
  Subagents    developer run -> auditor verdict -> (optional) adversary on critical tasks
  Memory       plain-text memory/LEDGER.md (append-only) + memory/STATE.md (snapshot)
  Reports      hourly REPORT-*.md + FINAL-REPORT.md
  Self-healing llama-swap health checks with service restart, stall watchdogs,
               bounded retries with auditor feedback, idle background queue

Usage:
  uv run --project env python conductor/conductor.py run conductor/missions/example.toml \
      --engine claude --hours 24
Stdlib only (Python >= 3.11 for tomllib).
"""

from __future__ import annotations

import argparse
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
       env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command in its own session so a watchdog kill takes the whole tree."""
    full_env = {**os.environ, **(env or {})}
    p = subprocess.Popen(args, cwd=cwd, env=full_env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, p.returncode, out or "", "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        return subprocess.CompletedProcess(args, -9, (out or "") + "\n[WATCHDOG] killed: timeout", "")


def git(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return sh(["git", "-C", str(repo), *args], timeout=timeout)


# ---------------------------------------------------------------- mission spec

@dataclass
class Task:
    id: str
    title: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    tier: str = "sonnet"
    timeout_minutes: int = 45
    max_attempts: int = 3
    adversary: bool = False          # extra adversarial pass after audit
    background: bool = False         # only runs when nothing else is dispatchable
    # runtime state
    status: str = "pending"          # pending|running|auditing|done|failed|blocked|merge-conflict
    attempts: int = 0
    note: str = ""


@dataclass
class Mission:
    name: str
    goal: str
    repo: Path
    tasks: list[Task]
    engine: str = "claude"
    hours: float = 24.0
    report_minutes: int = 60

    @staticmethod
    def load(path: Path, engine: str | None, hours: float | None) -> "Mission":
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        m = raw.get("mission", {})
        tasks = [Task(**{**t}) for t in raw.get("tasks", [])]
        tasks += [Task(**{**t, "background": True}) for t in raw.get("background", [])]
        ids = [t.id for t in tasks]
        assert len(ids) == len(set(ids)), "duplicate task ids"
        for t in tasks:
            for d in t.depends_on:
                assert d in ids, f"task {t.id} depends on unknown task {d}"
        repo = Path(os.path.expanduser(m.get("repo", str(ROOT)))).resolve()
        return Mission(
            name=m.get("name", path.stem),
            goal=m.get("goal", ""),
            repo=repo,
            tasks=tasks,
            engine=engine or m.get("engine", "claude"),
            hours=hours or float(m.get("hours", 24)),
            report_minutes=int(m.get("report_minutes", 60)),
        )


# ---------------------------------------------------------------- engines

def engine_cmd(engine: str, prompt: str, tier: str) -> tuple[list[str], dict]:
    """argv + extra env for one headless engine run (full autonomy: dedicated box)."""
    if engine == "claude":
        return (["bash", str(ROOT / "engines/claude-code/launch.sh"), "-p", prompt,
                 "--model", tier, "--output-format", "text",
                 "--dangerously-skip-permissions"], {})
    if engine == "opencode":
        return (["bash", str(ROOT / "engines/opencode/launch.sh"), "run",
                 "-m", f"oracle/{TIER_MODEL[tier]}", prompt],
                {"OPENCODE_PERMISSION": json.dumps(
                    {"edit": "allow", "bash": "allow", "webfetch": "deny"})})
    raise ValueError(f"unknown engine {engine!r} (use claude|opencode)")


# ---------------------------------------------------------------- conductor

class Conductor:
    def __init__(self, mission: Mission):
        self.m = mission
        self.t0 = time.monotonic()
        self.deadline = self.t0 + mission.hours * 3600
        self.lock = threading.Lock()
        self.current: str = "starting up"
        self.stop_reports = threading.Event()
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

        Dispatch is sequential and every task branches from the current mission
        tip, so this is always a fast-forward — implemented as a ref move
        (`branch -f`), which never touches whatever branch the operator has
        checked out in the main working copy."""
        if not self.use_git or wt == self.m.repo:
            return True
        git(wt, "add", "-A")
        git(wt, "commit", "-m", f"task({t.id}): {t.title} [audit: pass]")
        task_branch = self.task_branch(t)
        r = git(self.m.repo, "branch", "-f", self.branch, task_branch)
        if r.returncode != 0:
            t.status = "merge-conflict"
            self.ledger("MERGE ERROR", f"{t.id}: could not advance {self.branch} "
                                       f"({r.stdout.strip()[-300:]}); work kept in {wt}")
            return False
        git(self.m.repo, "worktree", "remove", "--force", str(wt))
        git(self.m.repo, "branch", "-D", task_branch)
        return True

    # ---- agents -------------------------------------------------------------
    def run_engine(self, prompt: str, tier: str, cwd: Path, timeout_min: int, tag: str) -> str:
        self.ensure_serving()
        argv, env = engine_cmd(self.m.engine, prompt, tier)
        r = sh(argv, cwd=cwd, timeout=timeout_min * 60, env=env)
        (LOGS / f"{self.m.name}-{tag}.log").write_text(r.stdout, encoding="utf-8")
        return r.stdout

    def developer_prompt(self, t: Task, feedback: str) -> str:
        acc = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(t.acceptance)) or "  (none listed)"
        fb = ""
        if feedback:
            fb = (f"\n\nATTEMPT {t.attempts} — THE PREVIOUS ATTEMPT FAILED:\n{feedback}\n"
                  "Do NOT re-run the failed approach harder. First write the DIAGNOSIS block in "
                  "TASKPLAN.md (root cause, not symptom — 5 lines), then execute a changed plan. "
                  "Repeating a logged failure wastes the mission's budget.")
        return (
            f"MISSION: {self.m.goal}\nTASK [{t.id}]: {t.title}\n\n{t.prompt}\n\n"
            f"ACCEPTANCE CRITERIA (audited independently — all must demonstrably hold):\n{acc}{fb}\n\n"
            "You operate under the Long-Horizon Autonomy Protocol (in your loaded instructions). "
            "Non-negotiables for this run:\n"
            f"1. START RITUAL before any edit: read {MEMORY / 'STATE.md'}, the tail of "
            f"{MEMORY / 'LEDGER.md'}, and {MEMORY / 'FAILURES.md'} (search '{t.id}'); "
            "run `git log --oneline -10` and `git status` here; read TASKPLAN.md if present.\n"
            "2. Write TASKPLAN.md (GOAL / 3-7 STEPS each with a CHECK / NOT-DOING) before "
            "touching code; keep it updated — it is your anchor against drift.\n"
            "3. Work the ratchet: one step, run its CHECK, commit, next step. Never end a "
            "step with a broken tree.\n"
            "4. Evidence standard: a criterion counts only with the command AND its fresh output.\n"
            "5. Re-anchor every ~10 actions: re-read the criteria and your current step.\n"
            f"6. Finish: full test suite from clean state, commit, ledger entry to "
            f"{MEMORY / 'LEDGER.md'} (what/why/files/next).\n"
            "If two genuinely different strategies fail, end with 'BLOCKED: <what you know, "
            "what you ruled out, what you would try next>'."
        )

    def audit(self, t: Task, wt: Path) -> tuple[bool, str]:
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
        out = self.run_engine(prompt, "haiku", wt, 20, f"{t.id}-audit{t.attempts}")
        m = re.findall(r"AUDIT:\s*(PASS|FAIL:?.*)", out)
        verdict = m[-1] if m else "FAIL: auditor produced no verdict"
        return verdict.startswith("PASS"), verdict

    def adversary_pass(self, t: Task, wt: Path) -> None:
        prompt = (
            f"You are the ADVERSARY. Attack the work for task [{t.id}] '{t.title}' in the "
            "current directory: edge cases, statistical validity, silent failures, wasted effort. "
            "Reproduce anything you claim. End with 'ADVERSARY: <c> critical, <m> major, <n> minor'."
        )
        out = self.run_engine(prompt, "opus", wt, 30, f"{t.id}-adversary")
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
            t.status = "running"
            self.current = f"task {t.id} (attempt {t.attempts}, {t.tier})"
            self.write_state()
            self.ledger(f"DISPATCH {t.id}", f"attempt {t.attempts}/{t.max_attempts}, tier {t.tier}")
            wt = self.make_worktree(t)
            out = self.run_engine(self.developer_prompt(t, feedback), t.tier, wt,
                                  t.timeout_minutes, f"{t.id}-dev{t.attempts}")
            if "[WATCHDOG] killed" in out:
                feedback = "Previous attempt hung and was killed. Decompose the work; commit incrementally."
                self.ledger(f"WATCHDOG {t.id}", "stalled run killed — retrying")
                self.log_failure(t, "watchdog-kill",
                                 "run exceeded its time box and was killed; the approach was "
                                 "likely too monolithic — decompose and commit incrementally")
                continue
            if re.search(r"^BLOCKED:", out.strip().splitlines()[-1] if out.strip() else "", re.M):
                t.status, t.note = "blocked", out.strip().splitlines()[-1][:300]
                self.ledger(f"BLOCKED {t.id}", t.note)
                self.log_failure(t, "blocked", t.note)
                return
            ok, verdict = self.audit(t, wt)
            self.ledger(f"AUDIT {t.id}", verdict[:300])
            if ok:
                if t.adversary:
                    self.adversary_pass(t, wt)
                if self.merge_task(t, wt):
                    t.status = "done"
                return
            self.log_failure(t, "audit-fail", verdict)
            feedback = verdict
        t.status, t.note = "failed", f"failed audit {t.max_attempts} times: {feedback[:200]}"
        self.ledger(f"FAILED {t.id}", t.note)
        self.log_failure(t, "exhausted", t.note)

    def dispatchable(self, background: bool) -> Task | None:
        done = {t.id for t in self.m.tasks if t.status == "done"}
        for t in self.m.tasks:
            if (t.background == background and t.status == "pending"
                    and all(d in done for d in t.depends_on)):
                return t
        return None

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
        while not self.stop_reports.wait(self.m.report_minutes * 60):
            p = self.report()
            self.ledger("HOURLY REPORT", str(p.relative_to(ROOT)))

    # ---- main loop ----------------------------------------------------------
    def run(self) -> int:
        self.ledger("MISSION START",
                    f"goal='{self.m.goal}' engine={self.m.engine} budget={self.m.hours}h "
                    f"tasks={len(self.m.tasks)} repo={self.m.repo}")
        if not self.use_git:
            self.ledger("WARN", "target repo is not a git repo — running without worktree isolation")
        reporter = threading.Thread(target=self._report_thread, daemon=True)
        reporter.start()
        try:
            while time.monotonic() < self.cutoff():
                self.write_state()
                task = self.dispatchable(False)
                if task:
                    self.run_task(task)
                    continue
                bg = self.dispatchable(True)
                if bg:
                    self.current = f"idle — running background task {bg.id}"
                    self.ledger("IDLE", f"no foreground work dispatchable; using time on {bg.id}")
                    self.run_task(bg)
                    continue
                pending = [t for t in self.m.tasks if t.status == "pending"]
                if not pending:
                    self.ledger("MISSION COMPLETE", "no pending tasks remain")
                    break
                self.current = f"stalled — {len(pending)} tasks blocked by failed dependencies"
                self.ledger("STALLED", self.current + " — ending early rather than idling")
                break
        except KeyboardInterrupt:
            self.ledger("INTERRUPTED", "operator stopped the mission")
        finally:
            self.stop_reports.set()
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
