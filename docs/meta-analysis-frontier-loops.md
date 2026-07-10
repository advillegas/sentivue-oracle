# Meta-analysis: frontier-agent build loops (boxing-poe vs mma-poe)

2026-07-09. Sources: full git histories, `PROGRESS.md` session logs, protocol and
verdict documents of two private production systems built by different frontier
models on near-identical problems:

- **boxing-poe** — built primarily with Opus 4.8 (147 commits): the original
  product, developed exploratorily over a longer period.
- **mma-poe** — built with Fable 5 (119 commits): a deliberate "carbon copy with
  domain deltas," zero-to-production in ~1 day, then multi-day autonomous ops loops
  with an orchestrator/panel structure.

Purpose: extract the process mechanisms that made the stronger loop strong, and
encode them into this repo's harness so local models inherit them. Frontier
behavior here is the baseline, not the ceiling.

## What the Opus 4.8 record shows

Strengths: exceptional depth once focused. The 2026-07-05 R&D loop is genuinely
rigorous — pre-registered promotion criteria, placebo/negative controls, disjoint-seed
confirmation, paired card-clustered inference, an owner-gated unseal that confirmed
the candidate, honest reporting.

Weaknesses visible in the history:

1. **Correctness converged through incidents.** The Gurrero orientation flip, the
   fabricated-draws label bug, promote-v3 → rollback-v2 → repromote, and reactive
   chains (`fix(critical)` → `fix(round2)` → `fix(round3)` → `fix(round3b)`) show
   invariants being discovered in production rather than designed in.
2. **Exploratory litter.** ~60 one-off `_check/_diagnose/_inspect` scripts with no
   read-only guarantee and no lifecycle.
3. **Rigor arrived late.** The strong methodology of the July R&D loop was built
   *after* months of incident-driven hardening; the early history operated without
   gates or pre-registration.

## What the Fable 5 record shows

The defining move: **the process was designed before the work.** A build manual was
written first, structured as an executable loop with numbered phase gates, each gate
requiring machine-checkable evidence, with a ledger (`PROGRESS.md`) as the single
source of truth and an explicit rule: never trust memory over the ledger.

Mechanisms observed working, with receipts:

1. **Regression re-verification.** `build_gates.py` re-verifies every
   previously-passed gate cheaply; "regressions reopen their phase." Done stays done.
2. **Pre-registration as default.** Promotion protocols, two-gate validation, look-N
   registrations — decision rules committed BEFORE any candidate ran ("NOTHING
   COMPUTED YET" appears verbatim in a registration commit).
3. **Honest negative verdicts.** "TWO-GATE ATTEMPT 1 VERDICT: HOLDOUT STAYS SEALED —
   Gate 1 FAIL, Gate 2 FAIL ... recorded honestly; sealed window untouched." Every
   point estimate favored the thesis; the bar wasn't met; the verdict says so.
4. **Attempt-count economics.** "The attempt count is itself part of the epistemic
   record. Repeated attempts erode gate protection; treat re-runs as expensive."
5. **Dry-run-first mutations.** DB-mutating logic ships as a dry-run script whose
   output is reviewed before the orchestrator executes the real mutation; mutations
   serialized; a three-phase integrator that "refuses --execute until the migration
   is applied."
6. **Role separation with adversaries.** Orchestrator merges only reviewed
   branches; developer agents confined to worktrees; a separate test-developer role
   pinning behavior with tests; adversarial audits that caught a CRITICAL seal leak
   ("refit_at_today now guards by seal cutoff DATE — 800 leaked into two live
   refits") and a HIGH phantom-row cleanup.
7. **Guardian countersign.** Irreversible ceremonies (unseal) blocked pending an
   explicit countersign; owner determinations recorded as dated, binding documents.
8. **Dated corrections, never silent edits.** "attempt-1 doc correction (dated,
   verdict unaffected)"; supersession trails numbered 1–8 on the cryptographic
   commitments, each with the old hash preserved.
9. **Resumable background jobs + watchers.** Batched, checkpointed scrapers ("full
   history walk no longer loses everything on interruption"); watcher scripts that
   arm the next stage when a long job lands.
10. **Incidents end in guards.** Every data-integrity event (5 logged on cutover
    day, "all logged, none silent") closed with a root cause, a code fix, AND a
    standing check or test.

## Differences that matter (Fable 5 vs Opus 4.8, as evidenced here)

| Dimension | Opus 4.8 (boxing) | Fable 5 (mma) |
|---|---|---|
| Process | emergent, incident-driven | designed first, executable manual + gates |
| Regressions | found in production | re-verified after every change; phases reopen |
| Evaluative decisions | rigor late (July loop) | pre-registered from day one, FAILs recorded |
| Mutations | direct, occasionally clobbering | dry-run-first, reviewed, serialized |
| Roles | single-threaded sessions | orchestrator / developers / test-dev / adversary / guardian |
| Corrections | fix-forward | dated addenda + supersession trails |
| Long jobs | vulnerable to restarts | checkpoint/resume + watchers |
| Knowledge transfer | implicit | "lessons encoded as requirements — do not relearn these" |

## What this repo adopted (implementation map)

| Finding | Where it landed |
|---|---|
| Regression re-verification | conductor `regression_sweep()`: after every merge, all done tasks' checks re-run on the mission branch; failures REOPEN the task with a repair attempt |
| Guardian countersign | task `requires_approval` + `memory/APPROVALS.md` gate; mission holds open (not stalled) while waiting; reports surface the queue |
| Dry-run-first + read-only probes | planner contract splits irreversible work into dry-run task → approval-gated execute task; developer prompt rule 5b; AUTONOMY §12 |
| Lessons as compounding memory | conductor `distill_lessons()` at mission end → `memory/LESSONS.md`; start ritual reads it ("do not relearn these") |
| Pre-registration + honest verdicts + attempt economics | AUTONOMY §12; quant-research skill "Pre-registration discipline" section |
| Incidents end in guards; dated corrections; resumable long jobs | AUTONOMY §12; planner contract requires launch/verify split for long jobs |

Already present before this analysis (independently converged with the mma-poe
record, which is encouraging): worktree isolation with merge-only mainline,
machine-checkable evidence gates, plain-text ledger as source of truth, failure
memory, auditor/adversary roles, escalation, watchdogs.

## Residual gaps (honest)

- mma-poe's panel had a *human owner* in the loop for scope decisions and
  determinations; our approvals file is the offline equivalent but is only as good
  as the operator's attention to `reports/`.
- The test-developer as a distinct role (tests written by an agent other than the
  implementer) is approximated by our auditor but not fully separated; a future
  refinement could add a `test-writer` subagent for high-stakes tasks.
- Fable 5's one-day zero-to-production pace also reflects raw model capability
  (planning coherence, tool reliability) that process cannot fully substitute for —
  the harness narrows this gap; the escalation ladder and verification stack are
  what close the rest on verifiable work.
