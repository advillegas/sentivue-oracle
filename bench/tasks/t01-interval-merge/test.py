import sys

try:
    from solution import merge_intervals
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

cases = [
    ([], []),
    ([(1, 3)], [(1, 3)]),
    ([(1, 3), (2, 6), (8, 10), (15, 18)], [(1, 6), (8, 10), (15, 18)]),
    ([(1, 2), (2, 3)], [(1, 3)]),
    ([(5, 6), (1, 2)], [(1, 2), (5, 6)]),
    ([(1, 10), (2, 3), (4, 5)], [(1, 10)]),
    ([(0, 0), (0, 0)], [(0, 0)]),
    ([(-5, -1), (-2, 4)], [(-5, 4)]),
]
for given, expected in cases:
    src = list(given)
    got = [tuple(x) for x in merge_intervals(src)]
    assert got == expected, f"{given}: expected {expected}, got {got}"
    assert src == list(given), "input was mutated"
print("ok")
sys.exit(0)
