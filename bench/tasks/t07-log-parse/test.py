import sys

try:
    from solution import parse_logs
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

text = """
2026-07-11T14:03:22Z INFO web: started on :8080
2026-07-11T14:03:23Z ERROR db: connect failed: timeout after 3s
2026-07-11T14:03:24Z WARN cache: miss rate 91%
totally not a log line
2026-07-11T14:03:25Z ERROR web: 500 on /api: null pointer

2026-07-11 14:03:26Z INFO web: bad timestamp separator
2026-07-11T14:03:27Z TRACE web: unknown level
2026-07-11T14:03:28Z INFO web-2: dash component ok
"""
out = parse_logs(text)
assert out["counts"] == {"INFO": 2, "ERROR": 2, "WARN": 1}, out["counts"]
assert out["errors"] == [
    ("db", "connect failed: timeout after 3s"),
    ("web", "500 on /api: null pointer"),
], out["errors"]
assert out["malformed"] == 3, out["malformed"]

empty = parse_logs("\n   \n")
assert empty["counts"] == {} and empty["errors"] == [] and empty["malformed"] == 0

print("ok")
sys.exit(0)
