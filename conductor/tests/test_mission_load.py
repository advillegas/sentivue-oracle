"""Mission.load invariants, using tmp TOML fixtures + the shipped missions."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conductor as C  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]


def write_mission(tmp_path, body):
    p = tmp_path / "m.toml"
    p.write_text(body, encoding="utf-8")
    return p


BASE = """
[mission]
name = "demo"
goal = "g"
workers = {workers}

[[tasks]]
id = "a"
title = "A"
prompt = "pa"
acceptance = ["a done"]
checks = ["python -V"]

[[tasks]]
id = "b"
title = "B"
prompt = "pb"
depends_on = ["a"]
research = true
acceptance = ["b done"]
checks = ["python -V"]
"""


def test_load_basics_and_research_flag(tmp_path):
    m = C.Mission.load(write_mission(tmp_path, BASE.format(workers=2)), None, None)
    assert m.name == "demo" and m.workers == 2
    assert m.tasks[1].depends_on == ["a"]
    assert m.tasks[1].research is True


def test_engine_hours_args_override_toml(tmp_path):
    body = BASE.format(workers=1).replace('goal = "g"', 'goal = "g"\nengine = "opencode"\nhours = 4')
    m = C.Mission.load(write_mission(tmp_path, body), "claude", 2.0)
    assert m.engine == "claude" and m.hours == 2.0


def test_workers_outside_supported_range_fail_closed(tmp_path):
    with pytest.raises(C.ConductorError):
        C.Mission.load(write_mission(tmp_path, BASE.format(workers=9)), None, None)
    with pytest.raises(C.ConductorError):
        C.Mission.load(write_mission(tmp_path, BASE.format(workers=0)), None, None)


def test_name_defaults_to_stem(tmp_path):
    body = BASE.format(workers=1).replace('name = "demo"\n', "")
    m = C.Mission.load(write_mission(tmp_path, body), None, None)
    assert m.name == "m"


def test_background_tasks_load_flagged(tmp_path):
    body = BASE.format(workers=1) + """
[[background]]
id = "bg"
title = "BG"
prompt = "pbg"
acceptance = ["bg done"]
checks = ["python -V"]
"""
    m = C.Mission.load(write_mission(tmp_path, body), None, None)
    bg = [t for t in m.tasks if t.background]
    assert [t.id for t in bg] == ["bg"]


def test_duplicate_ids_rejected(tmp_path):
    body = BASE.format(workers=1).replace('id = "b"', 'id = "a"')
    with pytest.raises(C.ConductorError):
        C.Mission.load(write_mission(tmp_path, body), None, None)


def test_unknown_dependency_rejected(tmp_path):
    body = BASE.format(workers=1).replace('depends_on = ["a"]', 'depends_on = ["ghost"]')
    with pytest.raises(C.ConductorError):
        C.Mission.load(write_mission(tmp_path, body), None, None)


def test_no_tasks_no_autoplan_rejected(tmp_path):
    with pytest.raises(C.ConductorError):
        C.Mission.load(write_mission(tmp_path, '[mission]\nname = "x"\ngoal = "g"\n'), None, None)


def test_goal_only_autoplan_accepted(tmp_path):
    m = C.Mission.load(write_mission(
        tmp_path, '[mission]\nname = "x"\ngoal = "g"\nauto_plan = true\n'), None, None)
    assert m.auto_plan and m.tasks == []


@pytest.mark.parametrize("mission_file", [
    "conductor/missions/example.toml",
    "conductor/missions/autonomous.toml",
    "conductor/missions/platform-hardening.toml",
    "conductor/missions/frontier-parity.toml",
])
def test_shipped_missions_load(mission_file):
    m = C.Mission.load(REPO / mission_file, None, None)
    assert m.tasks or m.auto_plan
