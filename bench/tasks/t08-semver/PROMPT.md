# Task: semantic version comparison

Implement `compare(a: str, b: str) -> int` in `solution.py` for semver strings.

- Returns -1 / 0 / 1 for a<b / a==b / a>b.
- Format: `MAJOR.MINOR.PATCH` with optional `-prerelease` suffix
  (dot-separated identifiers), e.g. `1.2.3`, `1.0.0-alpha.1`, `2.0.0-rc.2`.
- Numeric core compares numerically (`1.10.0 > 1.9.0`).
- A prerelease version is LOWER than its release (`1.0.0-rc.1 < 1.0.0`).
- Prerelease identifiers compare per semver: numeric identifiers compare as
  numbers and are lower than alphanumeric ones; otherwise ASCII order; a
  shorter identifier list is lower when all previous identifiers match
  (`1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta`).
- Invalid inputs (missing parts, non-numeric core, empty string) raise ValueError.
