# Task: interval merge

Implement `merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]`
in `solution.py`.

- Input: a list of closed integer intervals `(start, end)` with `start <= end`,
  in any order, possibly overlapping or touching.
- Output: the minimal list of merged intervals, sorted by start.
- Touching intervals merge: `(1, 2)` and `(2, 3)` -> `(1, 3)`.
- Empty input returns `[]`. Do not mutate the input list.
