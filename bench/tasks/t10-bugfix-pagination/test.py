import sys

try:
    from solution import paginate
except Exception as e:
    print("import failed:", e)
    sys.exit(1)

items = list(range(1, 11))  # 10 items

r = paginate(items, 1, 3)
assert r["items"] == [1, 2, 3] and r["pages"] == 4
assert r["has_prev"] is False and r["has_next"] is True

r = paginate(items, 4, 3)
assert r["items"] == [10], r["items"]          # final partial page exists
assert r["has_next"] is False and r["has_prev"] is True

r = paginate(items, 5, 3)                       # beyond the end
assert r["items"] == [] and r["pages"] == 4
assert r["has_next"] is False and r["has_prev"] is True

r = paginate([], 1, 10)
assert r["items"] == [] and r["pages"] == 0
assert r["has_next"] is False and r["has_prev"] is False

r = paginate(items, 1, 10)
assert r["items"] == items and r["pages"] == 1
assert r["has_next"] is False and r["has_prev"] is False

for bad_page, bad_per in [(0, 3), (-1, 3), (1, 0), (1, -2)]:
    try:
        paginate(items, bad_page, bad_per)
        raise SystemExit(f"expected ValueError for page={bad_page}, per_page={bad_per}")
    except ValueError:
        pass

print("ok")
sys.exit(0)
