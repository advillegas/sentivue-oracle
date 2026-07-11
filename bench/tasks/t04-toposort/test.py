import sys

try:
    from solution import toposort
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

assert toposort([]) == []
assert toposort([], nodes=["b", "a"]) == ["a", "b"]

out = toposort([("a", "b"), ("b", "c")])
assert out == ["a", "b", "c"]

# deterministic lexicographic tie-break
out = toposort([("a", "z"), ("b", "z")])
assert out == ["a", "b", "z"], out

out = toposort([("t", "x"), ("t", "y")], nodes=["m"])
assert out[0] == "m" or out == ["t", "m", "x", "y"] or out == ["m", "t", "x", "y"]
assert set(out) == {"t", "x", "y", "m"} and len(out) == 4
assert out.index("t") < out.index("x") and out.index("t") < out.index("y")
# strict determinism: smallest-ready-first
assert out == ["m", "t", "x", "y"], out

try:
    toposort([("a", "b"), ("b", "a")])
    raise SystemExit("expected ValueError on cycle")
except ValueError as e:
    assert "a" in str(e) or "b" in str(e)

# self-loop is a cycle
try:
    toposort([("s", "s")])
    raise SystemExit("expected ValueError on self-loop")
except ValueError:
    pass

print("ok")
sys.exit(0)
