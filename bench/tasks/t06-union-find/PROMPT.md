# Task: union-find (disjoint set)

Implement class `UnionFind` in `solution.py`:

- `union(a, b) -> bool`: merge the sets containing a and b; return True when a
  merge happened, False when they were already connected. Elements are created
  on first sight (any hashable).
- `connected(a, b) -> bool`: never creates elements; unknown elements are only
  connected to themselves (`connected(x, x)` is True even if x is unseen).
- `components() -> int`: number of disjoint sets among all SEEN elements (0 when
  nothing seen).
- Must handle 50k unions across 50k elements quickly (path compression or
  ranking required; the test enforces a time budget).
