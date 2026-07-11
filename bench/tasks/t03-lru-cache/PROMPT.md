# Task: LRU cache

Implement class `LRUCache(capacity: int)` in `solution.py`:

- `get(key) -> value | None`: returns the value and marks the key most-recently-used.
- `put(key, value) -> None`: inserts/updates and marks most-recently-used; when
  size would exceed capacity, evicts the least-recently-used key first.
- `keys() -> list`: current keys from least- to most-recently-used.
- `capacity <= 0` raises `ValueError` at construction.
- Updating an existing key must NOT evict anything.
