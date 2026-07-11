# Task: config file round-trip

Implement in `solution.py`:

- `save_config(path: str, config: dict) -> None`
- `load_config(path: str) -> dict`

Format (text file, UTF-8): one `key=value` per line, keys sorted
alphabetically. Values are typed: `int`, `float`, `bool`, `str`, and `None`
must survive a save/load round-trip EXACTLY (`load(save(c)) == c`, including
types: `"5"` stays str, `5` stays int, `True` stays bool, `None` stays None).

- Keys are non-empty strings without `=` or newlines; invalid keys raise ValueError.
- String values may contain `=`, spaces, and even newlines (escape them:
  the choice of escaping is yours, but the round-trip must be exact).
- `load_config` of a file written by `save_config` never guesses: types are
  encoded explicitly, not inferred from the shape of the text.
- Loading a missing file raises FileNotFoundError.
