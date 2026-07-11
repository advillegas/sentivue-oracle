"""Tests for bin/trace.py against synthetic session logs."""
import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("trace_cli", REPO / "bin" / "trace.py")
trace = importlib.util.module_from_spec(spec)
sys.modules["trace_cli"] = trace   # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(trace)


def make_log(path, *, tools=(), tokens=10, outcome="success", noise=True):
    lines = []
    if noise:
        lines += ["not json at all", "{broken json"]
    lines.append(json.dumps({"type": "system", "subtype": "init",
                             "model": "m", "cwd": "/x"}))
    content = [{"type": "text", "text": "working on it"}]
    for i, name in enumerate(tools):
        content.append({"type": "tool_use", "id": f"tu{i}", "name": name,
                        "input": {"command": f"run {i}"}})
    lines.append(json.dumps({"type": "assistant", "message": {
        "content": content, "usage": {"output_tokens": tokens}}}))
    lines.append(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu0", "is_error": False}]}}))
    lines.append(json.dumps({"type": "result", "subtype": outcome,
                             "is_error": outcome != "success",
                             "num_turns": 1, "duration_ms": 2000}))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_read_events_skips_malformed(tmp_path):
    log = make_log(tmp_path / "a.log", tools=["Bash"])
    events = trace.read_events(log)
    assert all(isinstance(e, dict) and e.get("type") for e in events)
    assert len(events) == 4  # init, assistant, user, result


def test_summarize_counts(tmp_path):
    log = make_log(tmp_path / "a.log", tools=["Bash", "Write", "Bash"], tokens=55)
    s = trace.summarize(trace.read_events(log))
    assert s.turns == 1
    assert s.tool_calls == 3
    assert s.tools == {"Bash": 2, "Write": 1}
    assert s.out_tokens == 55
    assert s.outcome == "success"


def test_summarize_error_outcome(tmp_path):
    log = make_log(tmp_path / "e.log", outcome="error_during_execution")
    assert trace.summarize(trace.read_events(log)).outcome == "error_during_execution/error"


def test_show_prints_tree_in_order(tmp_path, capsys):
    log = make_log(tmp_path / "a.log", tools=["Bash"])
    trace.cmd_show(str(log))
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("[init]")
    assert any("turn 1" in ln for ln in out)
    bash_lines = [ln for ln in out if "Bash(" in ln]
    assert bash_lines and "[ok]" in bash_lines[0]
    assert out[-1].startswith("[result] success")


def test_diff_reports_tool_deltas(tmp_path, capsys):
    a = make_log(tmp_path / "a.log", tools=["Bash", "Bash"])
    b = make_log(tmp_path / "b.log", tools=["Bash", "Write"])
    trace.cmd_diff(str(a), str(b))
    out = capsys.readouterr().out
    assert "tool Bash" in out
    assert "only in B: Write" in out


def test_grep_finds_and_caps(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(trace, "LOGS", tmp_path)
    make_log(tmp_path / "a.log", tools=["Bash"])
    trace.cmd_grep("tool_use", None)
    out = capsys.readouterr().out
    assert "a.log:" in out


def test_grep_no_match_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(trace, "LOGS", tmp_path)
    make_log(tmp_path / "a.log")
    trace.cmd_grep("zzz-never-present", None)
    assert "no matches" in capsys.readouterr().out


def test_list_inventory(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(trace, "LOGS", tmp_path)
    make_log(tmp_path / "m1-task-dev1.log", tools=["Bash"])
    make_log(tmp_path / "m2-task-dev1.log", outcome="error_during_execution")
    trace.cmd_list(None)
    out = capsys.readouterr().out
    assert "m1-task-dev1" in out and "m2-task-dev1" in out
    trace.cmd_list("m1")
    out2 = capsys.readouterr().out
    assert "m1-task-dev1" in out2 and "m2-task-dev1" not in out2
