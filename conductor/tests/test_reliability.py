"""Fail-safe conductor invariants.

These tests cover isolation, recovery, gates, and process ownership.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conductor as C  # noqa: E402
import console as Console  # noqa: E402

GIT = shutil.which("git")


def make_repo(path: pathlib.Path) -> pathlib.Path:
    path.mkdir()
    subprocess.run([GIT, "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        [GIT, "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        [GIT, "-C", str(path), "config", "user.name", "Conductor Test"],
        check=True,
    )
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [GIT, "-C", str(path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    return path


def isolate_runtime(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("MEMORY", "REPORTS", "LOGS", "WORKTREES"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setattr(C, name, directory)


def checked_task(task_id: str = "one", **values: object) -> C.Task:
    payload: dict[str, object] = {
        "id": task_id,
        "title": task_id.title(),
        "prompt": f"Implement {task_id}",
        "acceptance": [f"{task_id} is implemented"],
        "checks": [
            f'{sys.executable} -c "raise SystemExit(0)"',
        ],
    }
    payload.update(values)
    return C.Task.from_dict(payload)


def mission(repo: pathlib.Path, tasks: list[C.Task]) -> C.Mission:
    return C.Mission(
        name="reliability",
        goal="exercise fail-safe conductor behavior",
        repo=repo,
        tasks=tasks,
        engine="claude",
        hours=1.0,
        report_minutes=60,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"tier": "unknown"},
        {"checks": []},
        {"acceptance": []},
        {"max_attempts": 0},
        {"timeout_minutes": 0},
        {"adversary": "yes"},
        {"mystery": 1},
    ],
)
def test_malformed_task_settings_fail_closed(override: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "id": "task",
        "title": "Task",
        "prompt": "Do it",
        "acceptance": ["done"],
        "checks": ["python -V"],
    }
    payload.update(override)
    with pytest.raises(C.ConductorError):
        C.Task.from_dict(payload)


def test_plan_rejects_unknown_self_and_cyclic_dependencies() -> None:
    def block(tasks: list[dict[str, object]]) -> str:
        return "```json\n" + json.dumps(tasks) + "\n```"

    base = {
        "title": "Task",
        "prompt": "Do it",
        "acceptance": ["done"],
        "checks": ["python -V"],
    }
    assert C.parse_plan(block([{**base, "id": "a", "depends_on": ["ghost"]}])) == []
    assert C.parse_plan(block([{**base, "id": "a", "depends_on": ["a"]}])) == []
    assert (
        C.parse_plan(
            block(
                [
                    {**base, "id": "a", "depends_on": ["b"]},
                    {**base, "id": "b", "depends_on": ["a"]},
                ]
            )
        )
        == []
    )


@pytest.mark.skipif(GIT is None, reason="git required")
def test_worktree_creation_failure_is_fatal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    conductor = C.Conductor(mission(repo, [checked_task()]))
    real_git = C.git

    def denied_git(target: pathlib.Path, *args: str, **kwargs: object):
        if args[:2] == ("worktree", "add"):
            return subprocess.CompletedProcess(args, 1, "permission denied", "")
        return real_git(target, *args, **kwargs)

    monkeypatch.setattr(C, "git", denied_git)
    with pytest.raises(C.ConductorError, match="worktree"):
        conductor.make_worktree(conductor.m.tasks[0])


@pytest.mark.skipif(GIT is None, reason="git required")
def test_unbound_existing_mission_branch_requires_recovery(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    C.git(repo, "branch", "mission/reliability")

    with pytest.raises(C.ConductorError, match="mission branch"):
        C.Conductor(mission(repo, [checked_task()]))


@pytest.mark.skipif(GIT is None, reason="git required")
def test_dirty_unpublished_candidate_is_never_force_deleted(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task()
    conductor = C.Conductor(mission(repo, [task]))
    wt, branch = conductor.make_candidate_worktree(task, 1)
    assert wt is not None
    (wt / "valuable.txt").write_text("unpublished\n", encoding="utf-8")

    assert conductor.drop_worktree(wt, branch) is False
    assert wt.exists()
    assert C.git(repo, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0


@pytest.mark.skipif(GIT is None, reason="git required")
def test_checkpoint_resumes_done_work_without_replay(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    first = C.Conductor(mission(repo, [checked_task()]))
    first._ensure_mission_branch()
    assert C.git(repo, "commit", "--allow-empty", "-m", "complete one").returncode == 0
    completed = C.git(repo, "rev-parse", "HEAD").stdout.strip()
    assert C.git(repo, "branch", "-f", first.branch, completed).returncode == 0
    first.task_commits["one"] = completed
    first.m.tasks[0].status = "done"
    first.m.tasks[0].attempts = 1
    first.write_state()

    second = C.Conductor(mission(repo, [checked_task()]))
    assert second.run_id == first.run_id
    assert second.m.tasks[0].status == "done"
    assert second.m.tasks[0].attempts == 1


@pytest.mark.skipif(GIT is None, reason="git required")
def test_checkpoint_recovers_interrupted_attempt_as_pending(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    first = C.Conductor(mission(repo, [checked_task()]))
    first.m.tasks[0].status = "running"
    first.m.tasks[0].attempts = 1
    first.write_state()

    second = C.Conductor(mission(repo, [checked_task()]))
    assert second.m.tasks[0].status == "pending"
    assert second.m.tasks[0].attempts == 1
    assert "recovered" in second.m.tasks[0].note


@pytest.mark.skipif(GIT is None, reason="git required")
def test_checkpoint_restores_persisted_auto_plan(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")

    def auto_mission() -> C.Mission:
        return C.Mission(
            name="autoplan",
            goal="plan and recover",
            repo=repo,
            tasks=[],
            auto_plan=True,
            hours=1.0,
        )

    first = C.Conductor(auto_mission())
    first.m.tasks = [checked_task("planned")]
    first.write_state()

    second = C.Conductor(auto_mission())
    assert [task.id for task in second.m.tasks] == ["planned"]
    assert second.run_id == first.run_id


@pytest.mark.skipif(GIT is None, reason="git required")
def test_malformed_checkpoint_or_trace_fails_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    first = C.Conductor(mission(repo, [checked_task()]))
    first.write_state()
    first.trace_path.write_text("{broken\n", encoding="utf-8")

    with pytest.raises(C.ConductorError, match="trace"):
        C.Conductor(mission(repo, [checked_task()]))

    first.trace_path.write_text("", encoding="utf-8")
    first.checkpoint_path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(C.ConductorError, match="checkpoint"):
        C.Conductor(mission(repo, [checked_task()]))


@pytest.mark.skipif(GIT is None, reason="git required")
def test_trace_records_stable_run_task_and_attempt_ids(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    conductor = C.Conductor(mission(repo, [checked_task()]))
    conductor.proc("attempt", task="one", attempt=2, outcome="checks-fail")
    record = json.loads(conductor.trace_path.read_text(encoding="utf-8"))

    assert record["run_id"] == conductor.run_id
    assert record["task_id"] == f"{conductor.run_id}:one"
    assert record["attempt_id"] == f"{conductor.run_id}:one:2"


@pytest.mark.skipif(GIT is None, reason="git required")
def test_approval_is_bound_to_run_task_and_nonce(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(requires_approval=True)
    conductor = C.Conductor(mission(repo, [task]))
    approvals = C.MEMORY / "APPROVALS.md"
    approvals.write_text("APPROVE one\n", encoding="utf-8")
    assert conductor.approved(task) is False

    exact = conductor.approval_command(task)
    with approvals.open("a", encoding="utf-8") as handle:
        handle.write(exact + "\n")
    assert conductor.approved(task) is True


def test_console_only_surfaces_current_nonce_bound_approvals(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "MEMORY", tmp_path)
    record = {
        "run_id": "a" * 32,
        "task_id": "deploy",
        "generation": 1,
        "task_digest": "c" * 64,
        "nonce": "b" * 32,
        "command": (
            f"APPROVE {'a' * 32} deploy 1 {'c' * 64} {'b' * 32}"
        ),
        "deny_command": (
            f"DENY {'a' * 32} deploy 1 {'c' * 64} {'b' * 32}"
        ),
    }
    (tmp_path / "PENDING-APPROVALS.json").write_text(
        json.dumps({"schema_version": 2, "pending": [record]}),
        encoding="utf-8",
    )
    (tmp_path / "APPROVALS.md").write_text(
        "APPROVE deploy\n",
        encoding="utf-8",
    )
    assert Console.pending_approvals() == [record]
    with (tmp_path / "APPROVALS.md").open("a", encoding="utf-8") as handle:
        handle.write(record["command"] + "\n")
    assert Console.pending_approvals() == []


@pytest.mark.skipif(GIT is None, reason="git required")
def test_engine_prose_and_adversary_findings_cannot_bypass_gates(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(max_attempts=1, adversary=True)
    conductor = C.Conductor(mission(repo, [task]))
    monkeypatch.setattr(conductor, "ensure_serving", lambda: None)
    monkeypatch.setattr(
        conductor,
        "run_engine",
        lambda *args, **kwargs: "I changed files but omitted the required status",
    )
    checks_called = False

    def checks(*args: object, **kwargs: object) -> tuple[bool, str]:
        nonlocal checks_called
        checks_called = True
        return True, ""

    monkeypatch.setattr(conductor, "run_checks", checks)
    conductor.run_task(task)
    assert task.status == "failed"
    assert checks_called is False

    monkeypatch.setattr(
        conductor,
        "run_engine",
        lambda *args, **kwargs: "ADVERSARY: 1 critical, 0 major, 0 minor",
    )
    ok, verdict = conductor.adversary_pass(task, repo)
    assert ok is False
    assert "critical" in verdict

    monkeypatch.setattr(
        conductor,
        "run_engine",
        lambda *args, **kwargs: "ADVERSARY: 0 critical, 0 major, 1 minor",
    )
    ok, verdict = conductor.adversary_pass(task, repo)
    assert ok is False
    assert "minor" in verdict


@pytest.mark.skipif(GIT is None, reason="git required")
def test_auditor_mutation_is_isolated_and_fails_review(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task()
    conductor = C.Conductor(mission(repo, [task]))
    wt = conductor.make_worktree(task)
    (wt / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    C.git(wt, "add", "-A")
    C.git(wt, "commit", "-m", "candidate")

    def mutating_auditor(
        _prompt: str,
        _tier: str,
        cwd: pathlib.Path,
        _timeout: int,
        _tag: str,
        stall_min: int | None = None,
    ) -> str:
        del stall_min
        (pathlib.Path(cwd) / "auditor-write.txt").write_text(
            "mutation\n",
            encoding="utf-8",
        )
        return "AUDIT: PASS"

    monkeypatch.setattr(conductor, "run_engine", mutating_auditor)
    ok, verdict = conductor.audit(task, wt)
    assert ok is False
    assert "mutated" in verdict
    assert not (wt / "auditor-write.txt").exists()


@pytest.mark.skipif(GIT is None, reason="git required")
def test_missing_checks_and_failed_regression_gate_block_merge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    prior = checked_task("prior", checks=[f'{sys.executable} -c "raise SystemExit(1)"'])
    current = checked_task("current")
    conductor = C.Conductor(mission(repo, [prior, current]))
    prior.status = "done"
    assert conductor.run_checks(C.Task("x", "X", "x"), repo)[0] is False

    wt = conductor.make_worktree(current)
    (wt / "change.txt").write_text("change\n", encoding="utf-8")
    C.git(wt, "add", "-A")
    C.git(wt, "commit", "-m", "candidate")
    before = C.git(repo, "rev-parse", conductor.branch).stdout.strip()
    assert conductor.merge_task(current, wt) is False
    after = C.git(repo, "rev-parse", conductor.branch).stdout.strip()
    assert after == before
    assert current.status == "regression-failed"


@pytest.mark.skipif(GIT is None, reason="git required")
def test_infrastructure_failures_are_refunded_and_bounded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(max_attempts=1)
    conductor = C.Conductor(mission(repo, [task]))
    monkeypatch.setattr(conductor, "ensure_serving", lambda: None)
    monkeypatch.setattr(
        conductor, "run_engine", lambda *a, **k: "API Error: unavailable"
    )
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)

    conductor.run_task(task)
    assert task.status == "blocked"
    assert task.attempts == 0
    trace = [
        json.loads(line)
        for line in conductor.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len([row for row in trace if row["kind"] == "infra"]) == 3


def test_timeout_terminates_parent_and_child_processes() -> None:
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "print(p.pid,flush=True);time.sleep(30)"
    )
    started = time.monotonic()
    result = C.sh(
        [sys.executable, "-c", code],
        timeout=1,
        stall_timeout=10,
    )
    assert result.returncode == -9
    assert time.monotonic() - started < 15
    child_pid = int(result.stdout.splitlines()[0])
    deadline = time.monotonic() + 5
    while C._pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert C._pid_alive(child_pid) is False


def test_successful_parent_cannot_leave_orphaned_child() -> None:
    code = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "print(p.pid,flush=True)"
    )
    result = C.sh([sys.executable, "-c", code], timeout=10)
    assert result.returncode == 0
    child_pid = int(result.stdout.splitlines()[0])
    deadline = time.monotonic() + 5
    while C._pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert C._pid_alive(child_pid) is False


def test_singleton_lock_is_os_backed_and_reclaims_dead_owner(
    tmp_path: pathlib.Path,
) -> None:
    module_root = pathlib.Path(C.__file__).resolve().parent
    code = (
        "import pathlib,sys,time;"
        f"sys.path.insert(0,{str(module_root)!r});"
        "import conductor as C;"
        "C.ROOT=pathlib.Path(sys.argv[1]);"
        "C.acquire_singleton();"
        "print('LOCKED',flush=True);"
        "time.sleep(float(sys.argv[2]))"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "LOCKED"
        refused = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), "0"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert refused.returncode != 0
        assert "another conductor owns" in (refused.stdout + refused.stderr)
    finally:
        owner.kill()
        owner.wait(timeout=10)

    reclaimed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), "0"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert reclaimed.returncode == 0
    assert "LOCKED" in reclaimed.stdout


@pytest.mark.skipif(GIT is None, reason="git required")
def test_regression_commands_cannot_mutate_the_candidate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    mutating_check = (
        f'{sys.executable} -c "from pathlib import Path; '
        "Path('README.md').write_text('mutated\\\\n', encoding='utf-8')\""
    )
    prior = checked_task("prior", checks=[mutating_check])
    current = checked_task("current")
    conductor = C.Conductor(mission(repo, [prior, current]))
    prior.status = "done"
    prior.attempts = 1
    conductor._ensure_mission_branch()
    wt = conductor.make_worktree(current)
    (wt / "change.txt").write_text("candidate\n", encoding="utf-8")
    assert C.git(wt, "add", "-A").returncode == 0
    assert C.git(wt, "commit", "-m", "candidate").returncode == 0
    head = C.git(wt, "rev-parse", "HEAD").stdout.strip()

    ok, detail = conductor.run_regression_gates(current, wt)

    assert ok is False
    assert "mutat" in detail.lower()
    assert C.git(wt, "rev-parse", "HEAD").stdout.strip() == head
    assert C.git(wt, "status", "--porcelain").stdout.strip()


@pytest.mark.skipif(GIT is None, reason="git required")
def test_auditor_mutation_failure_is_never_tiebreakable(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(max_attempts=1)
    conductor = C.Conductor(mission(repo, [task]))
    monkeypatch.setattr(conductor, "ensure_serving", lambda: None)

    def completed_engine(
        _prompt: str,
        _tier: str,
        cwd: pathlib.Path,
        *_args: object,
        **_kwargs: object,
    ) -> str:
        pathlib.Path(cwd, "change.txt").write_text("candidate\n", encoding="utf-8")
        return "DONE"

    monkeypatch.setattr(conductor, "run_engine", completed_engine)
    monkeypatch.setattr(conductor, "run_checks", lambda *_args: (True, ""))
    audit_calls = 0

    def mutation_failure(*_args: object, **_kwargs: object) -> tuple[bool, str]:
        nonlocal audit_calls
        audit_calls += 1
        return False, "FAIL: auditor mutated its isolated review artifact"

    monkeypatch.setattr(conductor, "audit", mutation_failure)
    conductor.run_task(task)

    assert audit_calls == 1
    assert task.status == "failed"


@pytest.mark.skipif(GIT is None, reason="git required")
def test_tiebreak_infrastructure_failures_are_refunded_and_bounded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(max_attempts=1)
    conductor = C.Conductor(mission(repo, [task]))
    monkeypatch.setattr(conductor, "ensure_serving", lambda: None)
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)

    def completed_engine(
        _prompt: str,
        _tier: str,
        cwd: pathlib.Path,
        *_args: object,
        **_kwargs: object,
    ) -> str:
        pathlib.Path(cwd, "change.txt").write_text("candidate\n", encoding="utf-8")
        return "DONE"

    monkeypatch.setattr(conductor, "run_engine", completed_engine)
    monkeypatch.setattr(conductor, "run_checks", lambda *_args: (True, ""))
    audit_calls = 0

    def semantic_then_infra(
        *_args: object,
        **kwargs: object,
    ) -> tuple[bool, str]:
        nonlocal audit_calls
        audit_calls += 1
        if kwargs.get("tier") == "opus":
            return False, "FAIL: auditor infrastructure failure: HTTP 503"
        return False, "FAIL: auditor findings: semantic disagreement"

    monkeypatch.setattr(conductor, "audit", semantic_then_infra)
    conductor.run_task(task)

    assert audit_calls == 6
    assert task.status == "blocked"
    assert task.attempts == 0
    assert task.infra_strikes == 3


@pytest.mark.skipif(GIT is None, reason="git required")
def test_approval_binds_task_spec_and_is_consumed_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(requires_approval=True)
    conductor = C.Conductor(mission(repo, [task]))
    command = conductor.approval_command(task)
    (C.MEMORY / "APPROVALS.md").write_text(command + "\n", encoding="utf-8")
    assert conductor.approved(task) is True

    claimed = conductor.claim(False)
    assert claimed is task
    assert conductor.approved(task) is False

    task.prompt = "materially different irreversible operation"
    task.status = "pending"
    assert conductor.approved(task) is False
    assert conductor.approval_command(task) != command


@pytest.mark.skipif(GIT is None, reason="git required")
def test_exact_operator_denial_is_durable_and_blocks_dispatch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task(requires_approval=True)
    conductor = C.Conductor(mission(repo, [task]))
    denial = conductor.denial_command(task)
    (C.MEMORY / "APPROVALS.md").write_text(denial + "\n", encoding="utf-8")

    assert conductor.claim(False) is None
    assert task.status == "blocked"
    assert "denied" in task.note

    restored = C.Conductor(mission(repo, [checked_task(requires_approval=True)]))
    assert restored.m.tasks[0].status == "blocked"
    assert restored.claim(False) is None


@pytest.mark.skipif(GIT is None, reason="git required")
def test_checkpoint_rejects_valid_json_tampering_and_external_ref_movement(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    first = C.Conductor(mission(repo, [checked_task()]))
    first._ensure_mission_branch()
    first.write_checkpoint()
    payload = json.loads(first.checkpoint_path.read_text(encoding="utf-8"))
    payload["tasks"]["one"]["status"] = "done"
    first.checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(C.ConductorError, match="integrity"):
        C.Conductor(mission(repo, [checked_task()]))

    first.write_checkpoint()
    assert C.git(repo, "commit", "--allow-empty", "-m", "external move").returncode == 0
    moved = C.git(repo, "rev-parse", "HEAD").stdout.strip()
    assert C.git(repo, "branch", "-f", first.branch, moved).returncode == 0
    with pytest.raises(C.ConductorError, match="mission branch"):
        C.Conductor(mission(repo, [checked_task()]))


@pytest.mark.skipif(GIT is None, reason="git required")
def test_review_and_regression_worktree_creation_failures_are_fatal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task()
    conductor = C.Conductor(mission(repo, [task]))
    conductor._ensure_mission_branch()
    real_git = C.git

    def denied_git(target: pathlib.Path, *args: str, **kwargs: object):
        if args[:2] == ("worktree", "add"):
            return subprocess.CompletedProcess(args, 1, "permission denied", "")
        return real_git(target, *args, **kwargs)

    monkeypatch.setattr(C, "git", denied_git)
    with pytest.raises(C.ConductorError, match="review"):
        conductor.run_readonly_engine("review", "haiku", repo, 1, "review")

    task.status = "done"
    task.attempts = 1
    with pytest.raises(C.ConductorError, match="regression"):
        conductor.regression_sweep()


@pytest.mark.skipif(not C.IS_WIN, reason="Windows Job Object behavior")
def test_windows_job_assignment_failure_aborts_owned_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[int] = []
    real_terminate = C._terminate_process_tree

    def tracked_terminate(process: subprocess.Popen[str]) -> None:
        terminated.append(process.pid)
        real_terminate(process)

    monkeypatch.setattr(C, "_assign_windows_kill_job", lambda _process: None)
    monkeypatch.setattr(C, "_terminate_process_tree", tracked_terminate)
    with pytest.raises(C.ConductorError, match="Job Object"):
        C.sh([sys.executable, "-c", "import time; time.sleep(30)"], timeout=10)
    assert terminated


@pytest.mark.skipif(GIT is None, reason="git required")
def test_engine_invocations_have_unique_logs_and_physical_attempt_ids(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task()
    conductor = C.Conductor(mission(repo, [task]))
    task.attempts = 1
    monkeypatch.setattr(conductor, "ensure_serving", lambda: None)
    monkeypatch.setattr(
        C,
        "sh",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            "DONE\n",
            "",
        ),
    )

    conductor.run_engine("prompt", "sonnet", repo, 1, "one-dev1")
    conductor.run_engine("prompt", "sonnet", repo, 1, "one-dev1")

    records = [
        json.loads(line)
        for line in conductor.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    engines = [record for record in records if record["kind"] == "engine"]
    assert len(engines) == 2
    assert len({record["invocation_id"] for record in engines}) == 2
    assert len({record["attempt_id"] for record in engines}) == 2
    assert all(record["charged_attempt"] == 1 for record in engines)
    assert len(list((C.LOGS / conductor.run_id).glob("*.log"))) == 2


@pytest.mark.skipif(GIT is None, reason="git required")
def test_checkpoint_reconciles_crash_after_atomic_ref_update(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_runtime(tmp_path, monkeypatch)
    repo = make_repo(tmp_path / "repo")
    task = checked_task()
    first = C.Conductor(mission(repo, [task]))
    first._ensure_mission_branch()
    wt = first.make_worktree(task)
    (wt / "feature.txt").write_text("complete\n", encoding="utf-8")
    assert first.prepare_verification(task, wt)
    expected = C.git(repo, "rev-parse", first.branch).stdout.strip()
    tip = C.git(repo, "rev-parse", first.task_branch(task)).stdout.strip()
    task.status = "auditing"
    task.attempts = 1
    first.pending_merge = {
        "task_id": task.id,
        "expected_head": expected,
        "task_tip": tip,
    }
    first.write_checkpoint()
    assert C.git(
        repo,
        "update-ref",
        f"refs/heads/{first.branch}",
        tip,
        expected,
    ).returncode == 0

    second = C.Conductor(mission(repo, [checked_task()]))

    assert second.m.tasks[0].status == "done"
    assert second.task_commits["one"] == tip
    assert second.pending_merge is None
