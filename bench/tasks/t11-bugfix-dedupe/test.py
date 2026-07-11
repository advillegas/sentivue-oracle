import copy
import sys

try:
    from solution import dedupe_events
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

events = [
    {"id": "a", "ts": 1, "payload": "a1"},
    {"id": "b", "ts": 5, "payload": "b5"},
    {"id": "a", "ts": 9, "payload": "a9"},
    {"id": "c", "ts": 2, "payload": "c2"},
    {"id": "b", "ts": 3, "payload": "b3"},
]
snapshot = copy.deepcopy(events)

out = dedupe_events(events)
assert [e["id"] for e in out] == ["a", "b", "c"], out          # first-appearance order
assert out[0]["payload"] == "a9", out[0]                        # latest wins
assert out[1]["payload"] == "b5", out[1]                        # older duplicate ignored
assert events == snapshot, "input mutated"

assert dedupe_events([]) == []

one = [{"id": "x", "ts": 0, "payload": None}]
assert dedupe_events(one) == one and dedupe_events(one) is not one

try:
    dedupe_events([{"ts": 1, "payload": 0}])
    raise SystemExit("expected KeyError for missing id")
except KeyError as e:
    assert "id" in str(e)

try:
    dedupe_events([{"id": "x", "payload": 0}])
    raise SystemExit("expected KeyError for missing ts")
except KeyError as e:
    assert "ts" in str(e)

print("ok")
sys.exit(0)
