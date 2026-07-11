# Task: min-stack invariant

Implement class `MinStack` in `solution.py`:

- `push(x)`, `pop() -> x`, `peek() -> x`, `minimum() -> x`, `__len__`.
- `minimum()` returns the smallest value currently on the stack and must stay
  correct through any push/pop sequence, including duplicates of the minimum.
- `pop`/`peek`/`minimum` on an empty stack raise `IndexError`.
- All operations O(1) amortized (no scanning the stack in minimum()) — the
  tests exercise 100k operations and must finish quickly.
