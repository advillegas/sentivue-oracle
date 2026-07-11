import sys

try:
    from solution import compare
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

assert compare("1.2.3", "1.2.3") == 0
assert compare("1.2.3", "1.2.4") == -1
assert compare("1.10.0", "1.9.0") == 1
assert compare("2.0.0", "10.0.0") == -1

# prerelease < release
assert compare("1.0.0-rc.1", "1.0.0") == -1
assert compare("1.0.0", "1.0.0-rc.1") == 1

# semver spec ordering chain
chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
         "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
for lo, hi in zip(chain, chain[1:]):
    assert compare(lo, hi) == -1, (lo, hi)
    assert compare(hi, lo) == 1, (hi, lo)

assert compare("1.0.0-alpha.1", "1.0.0-alpha.1") == 0

for bad in ["", "1.2", "a.b.c", "1.2.x", "1..3"]:
    try:
        compare(bad, "1.0.0")
        raise SystemExit(f"expected ValueError for {bad!r}")
    except ValueError:
        pass

print("ok")
sys.exit(0)
