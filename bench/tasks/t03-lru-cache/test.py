import sys

try:
    from solution import LRUCache
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

try:
    LRUCache(0)
    raise SystemExit("expected ValueError for capacity 0")
except ValueError:
    pass

c = LRUCache(2)
c.put("a", 1)
c.put("b", 2)
assert c.get("a") == 1            # a becomes MRU
c.put("c", 3)                     # evicts b (LRU)
assert c.get("b") is None
assert c.get("a") == 1 and c.get("c") == 3
assert c.keys() == ["a", "c"]

c2 = LRUCache(2)
c2.put("x", 1)
c2.put("y", 2)
c2.put("x", 99)                   # update must not evict
assert c2.get("y") == 2 and c2.get("x") == 99
assert set(c2.keys()) == {"x", "y"}

c3 = LRUCache(1)
c3.put("k", "v")
c3.put("k2", "v2")
assert c3.get("k") is None and c3.get("k2") == "v2"
assert c3.keys() == ["k2"]

print("ok")
sys.exit(0)
