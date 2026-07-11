# Task: numerically stable running statistics

Implement class `RunningStats` in `solution.py` (Welford's algorithm or
equivalent — the tests punish naive sum-of-squares):

- `add(x: float)`, `count`, `mean`, `variance` (population), `stdev`.
- `mean`/`variance`/`stdev` on an empty instance raise ValueError; variance of
  a single value is 0.0.
- `merge(other) -> RunningStats`: NEW instance combining two stat streams
  (neither input mutated); merging with an empty instance works.
- Numerical stability: a million values of `1e9 + tiny_noise` must produce a
  small non-negative variance close to the noise variance — naive
  `E[x^2] - E[x]^2` catastrophically cancels and fails this.
