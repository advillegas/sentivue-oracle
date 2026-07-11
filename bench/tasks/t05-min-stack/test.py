import sys
import time

try:
    from solution import MinStack
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

s = MinStack()
for name in ("pop", "peek", "minimum"):
    try:
        getattr(s, name)()
        raise SystemExit(f"expected IndexError from {name} on empty stack")
    except IndexError:
        pass

s.push(5); s.push(3); s.push(7)
assert s.minimum() == 3 and s.peek() == 7 and len(s) == 3
assert s.pop() == 7
assert s.minimum() == 3
assert s.pop() == 3
assert s.minimum() == 5

# duplicate minimums must survive one pop of the min
s2 = MinStack()
s2.push(2); s2.push(2); s2.push(9)
s2.pop()
assert s2.minimum() == 2
s2.pop()
assert s2.minimum() == 2

# performance guard: 100k mixed ops well under a second if O(1)
t0 = time.time()
s3 = MinStack()
for i in range(100_000):
    s3.push(i % 1000)
    if i % 3 == 0:
        s3.minimum()
    if i % 5 == 0 and len(s3):
        s3.pop()
elapsed = time.time() - t0
assert elapsed < 2.0, f"too slow ({elapsed:.2f}s) - minimum() must be O(1)"

print("ok")
sys.exit(0)
