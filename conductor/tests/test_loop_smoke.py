"""End-to-end conductor smoke test: claim -> dispatch -> audit -> merge ->
report, with a scripted engine - no llama-swap, no network, no real models.
This is the loop's own regression net."""
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conductor as C  # noqa: E402

GIT = shutil.which("git")


def make_repo(path):
    path.mkdir()
    subprocess.run([GIT, "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run([GIT, "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run([GIT, "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(path), "add", "-A"], check=True)
    subprocess.run([GIT, "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


@pytest.mark.skipif(GIT is None, reason="git required")
def test_full_loop_offline(tmp_path, monkeypatch):
    t0 = time.monotonic()
    repo = make_repo(tmp_path / "repo")
    for name in ("MEMORY", "REPORTS", "LOGS", "WORKTREES"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(C, name, d)

    calls = []

    def scripted_engine(self, prompt, tier, cwd, timeout_min, tag, stall_min=None):
        calls.append(tag)
        if "You are the AUDITOR" in prompt:
            return "checked everything\nAUDIT: PASS"
        if "OVERSEER" in prompt or "historian" in prompt or "META-ANALYST" in prompt:
            return ""
        # developer: leave real work behind so the merge has content
        pathlib.Path(cwd, "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
        return "implemented feature.py\nDONE"

    monkeypatch.setattr(C.Conductor, "run_engine", scripted_engine)
    monkeypatch.setattr(C.Conductor, "ensure_serving", lambda self: None)
    monkeypatch.setattr(C.Conductor, "ensure_tools", lambda self: None)

    mission = C.Mission(
        name="smoke", goal="prove the loop", repo=repo,
        tasks=[C.Task(id="one", title="One", prompt="build feature.py")],
        engine="claude", hours=1.0, report_minutes=60,
    )
    rc = C.Conductor(mission).run()

    assert rc == 0
    assert mission.tasks[0].status == "done"

    ledger = (C.MEMORY / "LEDGER.md").read_text(encoding="utf-8")
    assert "DISPATCH one" in ledger
    assert "AUDIT one" in ledger and "PASS" in ledger
    assert "MISSION COMPLETE" in ledger
    assert (C.MEMORY / "STATE.md").exists()
    assert (C.REPORTS / "FINAL-REPORT.md").exists()

    # the audited work actually merged into the mission branch
    show = subprocess.run(
        [GIT, "-C", str(repo), "show", "mission/smoke:feature.py"],
        capture_output=True, text=True)
    assert show.returncode == 0 and "VALUE = 42" in show.stdout

    # dev ran before audit; the tags carry the task id
    assert any(t.startswith("one-dev") for t in calls)
    assert any("audit" in t for t in calls)
    assert time.monotonic() - t0 < 60, "smoke test must stay fast"


@pytest.mark.skipif(GIT is None, reason="git required")
def test_blocked_status_short_circuits(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo2")
    for name in ("MEMORY", "REPORTS", "LOGS", "WORKTREES"):
        d = tmp_path / (name.lower() + "2")
        d.mkdir()
        monkeypatch.setattr(C, name, d)

    def blocked_engine(self, prompt, tier, cwd, timeout_min, tag, stall_min=None):
        if "You are the AUDITOR" in prompt:
            return "AUDIT: PASS"
        if "OVERSEER" in prompt or "historian" in prompt or "META-ANALYST" in prompt:
            return ""
        return "tried\nBLOCKED: cannot proceed, missing fixture"

    monkeypatch.setattr(C.Conductor, "run_engine", blocked_engine)
    monkeypatch.setattr(C.Conductor, "ensure_serving", lambda self: None)
    monkeypatch.setattr(C.Conductor, "ensure_tools", lambda self: None)

    mission = C.Mission(
        name="smoke2", goal="g", repo=repo,
        tasks=[C.Task(id="one", title="One", prompt="p")],
        engine="claude", hours=1.0,
    )
    rc = C.Conductor(mission).run()
    assert rc == 1
    assert mission.tasks[0].status == "blocked"
    assert mission.tasks[0].attempts == 1, "BLOCKED must not burn retries"
    failures = (C.MEMORY / "FAILURES.md").read_text(encoding="utf-8")
    assert "blocked" in failures
