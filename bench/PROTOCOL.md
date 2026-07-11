# Frontier-Parity Benchmark Protocol (pre-registered)

Frozen: 2026-07-11, BEFORE any scored run (seed brain V5). Amendments to this
protocol close at first computation of any registered quantity (C10); changes
after that require a new protocol version and a fresh baseline.

## Thesis under test

"Frontier-level capability is a property of the SYSTEM, not the model"
(SEED-BRAIN, closing thesis). We cannot compare against hosted frontier models
offline — and do not need to. The registered claim is about the HARNESS LIFT:

> The verified loop (checks -> independent audit -> retry-with-feedback ->
> best-of-N) must lift task pass rate far above the raw model's single-shot
> rate, and that lift must persist or grow mission-over-mission.

## Registered metrics

- **RAW**: single-shot engine pass rate — one headless engine run per task,
  no retries, no verification (`python bench/run.py --mode raw`).
- **HARNESSED**: pass rate under mission semantics — up to 3 attempts with
  check feedback (`--mode harnessed`), emulating the conductor's retry ladder.
- **LIFT** = HARNESSED − RAW (percentage points). The frontier-parity tracking
  number. Registered success criterion for the first measurement: LIFT >= +25pp
  on the 12-task suite with the 30B tier, calibration clean.

## Instrument calibration (V11 — before believing any measurement)

- `--mode reference` must score **12/12** (reference solutions prove every test
  is satisfiable) — run before every scored session.
- `--mode placebo` must score **0/12** (an empty solution set through the
  identical pipeline must fail every test). A placebo pass = broken test.

## Rules

- Tasks are fully offline, stdlib-only Python 3.12, deterministic tests.
- No task may be edited after its first scored run (its ID retires instead).
- Every scored run appends one line to `bench/RESULTS.jsonl` (append-only):
  mode, model, per-task outcomes, durations, git SHA of bench/ at run time.
- Results publish at equal prominence regardless of sign (V7). A LIFT below
  target is a finding about the harness, recorded in the ledger and FAILURES.
- Multiplicity discipline (V8): scored sessions are counted looks; re-running
  until a good number appears is gaming and forbidden — one scored session per
  mission, telemetry-logged.

## Suite composition (12 tasks, authored by the frontier-parity mission)

algorithmic (3) · data-structure invariants (2) · parsing/regex (2) ·
file-IO round-trip (1) · bug-fix on provided broken code (2) ·
refactor-preserving-tests (1) · numerics/edge cases (1)

Two seed tasks (`t01`, `t02`) ship with the runner as authoring patterns and
runner-validation fixtures; the mission authors the remaining ten plus
references, then runs calibration and the first scored baseline.
