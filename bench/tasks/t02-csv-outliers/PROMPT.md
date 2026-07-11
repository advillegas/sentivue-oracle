# Task: CSV outlier report

Implement `outliers(csv_text: str, k: float = 2.0) -> list[str]` in `solution.py`.

- `csv_text` is CSV with header `name,value`; values are floats; rows may have
  surrounding whitespace; blank lines are skipped.
- Return the names (in input order) whose value differs from the mean by more
  than `k` population standard deviations (strictly greater).
- If there are fewer than 2 data rows, or the standard deviation is 0, return `[]`.
- Malformed rows (missing value, non-numeric) raise `ValueError` naming the
  offending line number (1-based, counting data rows from 1).
