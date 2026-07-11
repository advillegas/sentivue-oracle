# Task: fix the pagination bug (bug-fix task — broken code provided)

The function below ships in `solution.py` — copy it there and FIX it. Its
contract: `paginate(items: list, page: int, per_page: int) -> dict` returns

- `"items"`: the items on 1-BASED page `page`
- `"pages"`: total page count (0 for an empty list)
- `"has_next"` / `"has_prev"`: booleans
- `page < 1` or `per_page < 1` raise `ValueError`
- a `page` beyond the last returns an empty `"items"` (with correct flags),
  never raises

Broken implementation to fix:

```python
def paginate(items, page, per_page):
    pages = len(items) // per_page          # BUG: drops the final partial page
    start = page * per_page                 # BUG: treats page as 0-based
    chunk = items[start:start + per_page]
    return {
        "items": chunk,
        "pages": pages,
        "has_next": page < pages,           # BUG: wrong off the 0/1-based confusion
        "has_prev": page > 1,
    }
```

Fix the bugs; keep the function signature and return shape identical. Add the
missing input validation.
