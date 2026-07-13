import sys

try:
    from solution import RunningStats
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

s = RunningStats()
for name in ("mean", "variance", "stdev"):
    try:
        getattr(s, name)
        raise SystemExit(f"expected ValueError from empty {name}")
    except ValueError:
        pass

s.add(4.0)
assert s.count == 1 and s.mean == 4.0 and s.variance == 0.0

for x in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]:
    if s.count == 1:
        s2 = RunningStats()
        s2.add(2.0)
# simple known distribution: 2,4,4,4,5,5,7,9 -> mean 5, pop var 4, stdev 2
t = RunningStats()
for x in [2, 4, 4, 4, 5, 5, 7, 9]:
    t.add(float(x))
assert t.count == 8
assert abs(t.mean - 5.0) < 1e-12
assert abs(t.variance - 4.0) < 1e-12
assert abs(t.stdev - 2.0) < 1e-12

# merge: two halves equal the whole
a, b, whole = RunningStats(), RunningStats(), RunningStats()
for x in [1.0, 2.0, 3.0]:
    a.add(x)
    whole.add(x)
for x in [10.0, 20.0]:
    b.add(x)
    whole.add(x)
m = a.merge(b)
assert m.count == whole.count
assert abs(m.mean - whole.mean) < 1e-9
assert abs(m.variance - whole.variance) < 1e-9
assert a.count == 3 and b.count == 2, "merge must not mutate inputs"

empty = RunningStats()
m2 = t.merge(empty)
assert m2.count == t.count and abs(m2.variance - t.variance) < 1e-12

# stability: naive E[x^2]-E[x]^2 fails here
big = RunningStats()
for i in range(1_000_000):
    big.add(1e9 + (i % 7) * 1e-3)
assert big.variance >= 0.0, "variance went negative - naive formula"
assert 1e-6 < big.variance < 1e-4, f"variance {big.variance} implausible"

print("ok")
sys.exit(0)
