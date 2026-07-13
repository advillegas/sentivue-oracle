"""Pure-logic tests for the conductor: plan parsing, result extraction, task
field clamping. No git, no network, no models."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import conductor as C  # noqa: E402


def plan_block(tasks):
    return "preamble\n```json\n" + json.dumps(tasks) + "\n```\ntrailer"


def task(i, **kw):
    d = {
        "id": f"t{i}",
        "title": f"T{i}",
        "prompt": f"do {i}",
        "acceptance": [f"task {i} is complete"],
        "checks": ["python -V"],
    }
    d.update(kw)
    return d


# ---- parse_plan ---------------------------------------------------------------

def test_parse_plan_valid_roundtrip():
    out = C.parse_plan(plan_block([task(1), task(2, depends_on=["t1"], tier="haiku")]))
    assert [t.id for t in out] == ["t1", "t2"]
    assert out[1].depends_on == ["t1"]
    assert out[1].tier == "haiku"


def test_parse_plan_invalid_json_returns_empty():
    assert C.parse_plan("```json\n{not json]\n```") == []


def test_parse_plan_no_block_returns_empty():
    assert C.parse_plan("no fenced block here") == []


def test_parse_plan_empty_array_returns_empty():
    assert C.parse_plan(plan_block([])) == []


def test_parse_plan_duplicate_ids_rejects_block():
    assert C.parse_plan(plan_block([task(1), task(1)])) == []


def test_parse_plan_rejects_unknown_and_self_deps():
    assert C.parse_plan(
        plan_block([task(1, depends_on=["ghost"]), task(2)])
    ) == []
    assert C.parse_plan(
        plan_block([task(1, depends_on=["t1"]), task(2)])
    ) == []


def test_parse_plan_rejects_more_than_ten():
    out = C.parse_plan(plan_block([task(i) for i in range(15)]))
    assert out == []


def test_parse_plan_last_valid_block_wins():
    text = plan_block([task(1)]) + "\n" + plan_block([task(2)])
    out = C.parse_plan(text)
    assert [t.id for t in out] == ["t2"]


def test_parse_plan_rejects_entries_missing_required_fields():
    out = C.parse_plan(plan_block([{"id": "a"}, {"prompt": "p"}, task(3)]))
    assert out == []


def test_parse_plan_last_block_invalid_falls_back_to_earlier():
    text = plan_block([task(1)]) + "\n```json\n[{]\n```"
    out = C.parse_plan(text)
    assert [t.id for t in out] == ["t1"]


# ---- extract_result -----------------------------------------------------------

def test_extract_result_picks_final_result_event():
    raw = "\n".join([
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({"type": "result", "result": "first"}),
        json.dumps({"type": "result", "result": "second"}),
    ])
    assert C.extract_result("claude", raw) == "second"


def test_extract_result_falls_back_to_tail_without_result_event():
    raw = "x" * 5000
    out = C.extract_result("claude", raw)
    assert out == raw[-4000:]


def test_extract_result_ignores_unparseable_lines():
    raw = "not json\n" + json.dumps({"type": "result", "result": "ok"}) + "\n{broken"
    assert C.extract_result("claude", raw) == "ok"


def test_extract_result_opencode_passthrough():
    assert C.extract_result("opencode", "plain text out") == "plain text out"


def test_extract_result_kilo_passthrough():
    assert C.extract_result("kilo", "kilo text out") == "kilo text out"


# ---- engine_cmd ---------------------------------------------------------------

def test_engine_cmd_kilo_autonomous_local_model():
    argv, env = C.engine_cmd("kilo", "do the thing", "sonnet")
    joined = " ".join(argv)
    assert "engines" in joined and "kilo" in joined       # repo launcher, not bare binary
    assert "--auto" in argv                               # headless autonomous mode
    mi = argv.index("-m")
    assert argv[mi + 1] == f"openai-compatible/{C.TIER_MODEL['sonnet']}"
    assert argv[-1] == "do the thing"
    assert env == {}


def test_engine_cmd_unknown_engine_raises():
    try:
        C.engine_cmd("roo", "x", "sonnet")
    except ValueError as e:
        assert "kilo" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown engine")


# ---- Task.from_dict -----------------------------------------------------------

def test_from_dict_rejects_unknown_keys():
    with pytest.raises(C.ConductorError):
        C.Task.from_dict({**task(1), "mystery_field": 42})


def test_from_dict_rejects_bad_tiers():
    with pytest.raises(C.ConductorError):
        C.Task.from_dict(task(1, tier="galaxy", audit_tier="galaxy"))


def test_from_dict_rejects_out_of_range_timeout():
    with pytest.raises(C.ConductorError):
        C.Task.from_dict(task(1, timeout_minutes=999))


def test_from_dict_rejects_out_of_range_best_of_n():
    with pytest.raises(C.ConductorError):
        C.Task.from_dict(task(1, best_of_n=9))
    with pytest.raises(C.ConductorError):
        C.Task.from_dict(task(1, best_of_n=0))
    assert C.Task.from_dict(task(1)).best_of_n == 1


def test_from_dict_booleans_and_background():
    t = C.Task.from_dict(task(1, research=True, requires_approval=True,
                              adversary=True), background=True)
    assert t.research and t.requires_approval and t.adversary and t.background


def test_seed_brain_index_nonempty_and_shaped():
    idx = C.seed_brain_index()
    lines = idx.splitlines()
    assert len(lines) > 80
    assert all(line.split(" ")[0][0] in "OALCEVGM" for line in lines if line)
