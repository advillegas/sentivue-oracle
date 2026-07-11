import sys
import time

try:
    from solution import UnionFind
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

u = UnionFind()
assert u.components() == 0
assert u.connected("ghost", "ghost") is True
assert u.connected("a", "b") is False
assert u.components() == 0, "connected() must not create elements"

assert u.union("a", "b") is True
assert u.union("a", "b") is False
assert u.connected("a", "b") is True
assert u.components() == 1

u.union("c", "d")
assert u.components() == 2
u.union("b", "c")
assert u.components() == 1
assert u.connected("a", "d")

u.union("e", "e")
assert u.components() == 2

t0 = time.time()
big = UnionFind()
for i in range(50_000):
    big.union(i, i + 1)
assert big.connected(0, 50_000)
assert big.components() == 1
for i in range(0, 50_000, 7):
    assert big.connected(i, 50_000 - (i % 3))
elapsed = time.time() - t0
assert elapsed < 3.0, f"too slow ({elapsed:.2f}s) - use path compression/rank"

print("ok")
sys.exit(0)
