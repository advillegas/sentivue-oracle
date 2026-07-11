#!/usr/bin/env python3
"""Frontier-parity benchmark runner (see bench/PROTOCOL.md - pre-registered).

Modes:
  reference  run each task's reference solution against its tests (must be N/N)
  placebo    run an empty solution against every test (must be 0/N)
  raw        one single-shot engine run per task, no feedback
  harnessed  up to 3 engine attempts per task with failing-test feedback

Task layout:  bench/tasks/<id>/
  PROMPT.md      what the engine is asked to build (writes solution.py)
  test.py        deterministic stdlib test; exit 0 = pass; imports solution
  reference.py   known-good solution (calibration only; never shown to engines)

Results append to bench/RESULTS.jsonl (append-only, per protocol).
Stdlib only; engine invocation mirrors the conductor's headless runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
IS_WIN = os.name == "nt"
ATTEMPTS_HARNESSED = 3
RUN_TIMEOUT_MIN = 25


def engine_argv(prompt: str) -> list[str]:
    name = "claude-code"
    launcher = ([
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / f"engines/{name}/launch.ps1")] if IS_WIN
        else ["bash", str(ROOT / f"engines/{name}/launch.sh")])
    return launcher + ["-p", prompt, "--model", "sonnet",
                       "--output-format", "stream-json", "--verbose",
                       "--include-partial-messages", "--dangerously-skip-permissions"]


def run_tests(task: Path, solution: Path) -> tuple[bool, str]:
    """Copy tests + solution into a scratch dir; run test.py; exit 0 = pass."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        shutil.copy(task / "test.py", scratch / "test.py")
        if solution.exists():
            shutil.copy(solution, scratch / "solution.py")
        r = subprocess.run([sys.executable, "test.py"], cwd=scratch,
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def engine_solve(task: Path, workdir: Path, feedback: str, tag: str) -> None:
    prompt = (
        f"Create the file solution.py in the CURRENT DIRECTORY ({workdir}) solving the "
        f"task below. Write ONLY solution.py; do not create tests or other files.\n\n"
        f"{(task / 'PROMPT.md').read_text(encoding='utf-8')}\n"
        + (f"\nYOUR PREVIOUS ATTEMPT FAILED THESE TESTS:\n{feedback}\n"
           "Fix the actual root cause." if feedback else "")
    )
    r = subprocess.run(engine_argv(prompt), cwd=workdir, capture_output=True,
                       text=True, timeout=RUN_TIMEOUT_MIN * 60)
    (BENCH / "logs").mkdir(exist_ok=True)
    (BENCH / "logs" / f"{tag}.log").write_text(r.stdout[-200000:], encoding="utf-8")


def score(mode: str) -> dict:
    tasks = sorted(d for d in (BENCH / "tasks").iterdir() if (d / "test.py").exists())
    if not tasks:
        sys.exit("no tasks under bench/tasks")
    outcomes: dict[str, bool] = {}
    t0 = time.time()
    for task in tasks:
        tid = task.name
        if mode == "reference":
            ok, _ = run_tests(task, task / "reference.py")
        elif mode == "placebo":
            ok, _ = run_tests(task, task / "does-not-exist.py")
        else:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                feedback = ""
                ok = False
                rounds = 1 if mode == "raw" else ATTEMPTS_HARNESSED
                for attempt in range(1, rounds + 1):
                    engine_solve(task, work, feedback, f"{mode}-{tid}-a{attempt}")
                    ok, out = run_tests(task, work / "solution.py")
                    if ok:
                        break
                    feedback = out
        outcomes[tid] = ok
        print(f"  {tid}: {'PASS' if ok else 'FAIL'}", flush=True)
    passed = sum(outcomes.values())
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode, "passed": passed, "total": len(tasks),
        "outcomes": outcomes, "secs": round(time.time() - t0),
        "bench_sha": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True).stdout.strip(),
    }
    with (BENCH / "RESULTS.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"{mode}: {passed}/{len(tasks)}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True,
                    choices=["reference", "placebo", "raw", "harnessed"])
    args = ap.parse_args()
    rec = score(args.mode)
    if args.mode == "reference" and rec["passed"] != rec["total"]:
        print("CALIBRATION FAIL: reference solutions must pass everything (V11)")
        return 1
    if args.mode == "placebo" and rec["passed"] != 0:
        print("CALIBRATION FAIL: placebo must score zero (V11)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
