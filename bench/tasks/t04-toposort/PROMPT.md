# Task: topological sort with deterministic tie-breaking

Implement `toposort(edges: list[tuple[str, str]], nodes: list[str] | None = None) -> list[str]`
in `solution.py`.

- `edges` are `(before, after)` dependencies: `before` must appear earlier.
- `nodes` optionally lists extra nodes with no edges; the result must include
  every node mentioned anywhere, exactly once.
- Ties (multiple ready nodes) break LEXICOGRAPHICALLY (smallest first) so the
  output is deterministic.
- A cycle raises `ValueError` mentioning at least one node in the cycle.
