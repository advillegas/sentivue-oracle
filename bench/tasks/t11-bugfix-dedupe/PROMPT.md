# Task: fix the deduplication bug (bug-fix task — broken code provided)

The function below ships in `solution.py` — copy it there and FIX it. Contract
for `dedupe_events(events: list[dict]) -> list[dict]`:

- Events are dicts with keys `id` (str), `ts` (int), `payload` (anything).
- Two events are duplicates when they share the same `id`; KEEP the one with
  the HIGHEST `ts` (latest wins). Order of the result: by first appearance of
  each id in the input.
- The input list and its dicts must NOT be mutated.
- Events missing `id` or `ts` raise `KeyError` (mentioning the missing key).

Broken implementation to fix:

```python
def dedupe_events(events):
    seen = {}
    for ev in events:
        if ev["id"] not in seen or ev["ts"] < seen[ev["id"]]["ts"]:   # BUG: keeps OLDEST
            seen[ev["id"]] = ev
    return list(seen.values())    # BUG: insertion order breaks when a later ts
                                  # replaces an earlier id (and mutation risk elsewhere)
```

Fix latest-wins and stable first-appearance ordering; keep the signature.
