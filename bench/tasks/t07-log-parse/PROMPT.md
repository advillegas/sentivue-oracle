# Task: structured log parsing

Implement `parse_logs(text: str) -> dict` in `solution.py`.

Input lines look like:
`2026-07-11T14:03:22Z LEVEL component: message`
where LEVEL is DEBUG/INFO/WARN/ERROR (anything else makes the line malformed).

Return a dict:
- `"counts"`: dict LEVEL -> number of well-formed lines with that level
  (only levels that appear).
- `"errors"`: list of `(component, message)` tuples for ERROR lines, in order.
- `"malformed"`: count of non-empty lines that do not match the format
  (blank/whitespace-only lines are ignored entirely).

The component never contains spaces; the message may contain anything
(including colons). Timestamps must match the exact shape above
(`YYYY-MM-DDTHH:MM:SSZ`) or the line is malformed.
