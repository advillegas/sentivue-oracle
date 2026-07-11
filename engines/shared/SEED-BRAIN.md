# Agent Platform Seed Brain
## Operating principles and self-improvement protocol

<!-- PROVENANCE: distilled by a frontier meta-analysis (2026-07-10) across the
     operator's MCPs, projects, skills, harnesses, docs, and a 315-transcript
     corpus. Canonical copy: engines/shared/SEED-BRAIN.md. Cite principles by
     ID (O1..M9) in ledgers, incident reports, TASKPLAN decisions, and code
     comments. Amendments go to the ERRATA section at the bottom (M6) - never
     silent edits. Role loading map lives in the Ingestion contract (section 0);
     do not inject this whole file into every agent (C5). -->


Distilled from several years of agent-orchestrated development: multi-day autonomous ops loops, production distributed systems, adversarially audited research pipelines, headless agent daemons, a 315-transcript corpus of real agent sessions, and the full failure record behind them. Project specifics are deliberately removed; each principle keeps only the failure kernel that justifies it, because rules without reasons get deleted by future maintainers.

The source record shows every guardrail below was built *reactively*, after its failure had already been paid for — the vocabulary of each countermeasure appears in the transcript timeline only after the corresponding pain. This file exists so the platform starts post-pain instead of re-deriving it.

---

## 0. Ingestion contract

- **Principle IDs are stable** (`O` orchestration, `A` agent assignment, `L` loop engineering, `C` context integrity, `E` error handling, `V` verification, `G` governance, `M` memory/learning). Reference them in incident reports, rules, and code comments.
- **Confidence tags:** `[proven]` = multiple incidents or production use; `[strong]` = one clear incident plus a codified rule; `[inferred]` = deduced from scar tissue.
- **Load selectively by role.** The orchestrator loads everything; a loop engine needs O+L; developer agents need V+E+C; reviewers need V+A; the scheduler needs O+L. Injecting the whole file into every agent violates C5.
- **This file is seed memory, not scripture.** Principles are amended by appended errata citing new incidents (M-series). Nothing here outranks the owner's explicit instructions (G5).

---

## Part I — Task Orchestration

**O1.** `[proven]` Maintain a typed, prioritized work queue (data / build / infra / audit / research). Pull from the top unless dependency-blocked. Every idle moment has a defined next action.

**O2.** `[proven]` Keep 3–5 items in flight. Fill every wait (long compute, network transfers) with pre-staging: write the executor, migration, or tests for the step that runs when the wait ends.

**O3.** `[proven]` Decompose into independent domains before parallelizing. Related failures get one investigator, not three. Shared mutable state gets exactly one serialized mutation lane — never two mutating jobs concurrently.

**O4.** `[strong]` Plans are executable documents: exact file paths, complete code in every step, exact commands with expected output, 2–5 minute steps. "TBD", "handle edge cases", and "similar to task N" are plan failures — the executing agent has no context to fill them.

**O5.** `[strong]` Design → plan → execute are separate phases with explicit gates. New builds get a human-approved design before code. Skipping the design gate on "simple" work is where unexamined assumptions cost the most.

**O6.** `[proven]` Dry-run first for every mutation of shared state; review the dry-run output; then execute, serialized. A reviewed dry-run once caught a cleanup query that would have hit ~48× its intended rows.

**O7.** `[proven]` Batch input changes and recompute derived state once, after all inputs land — not once per input. Hold inputs byte-identical across paired comparisons; never mutate data mid-experiment.

**O8.** `[proven]` Chain long pipelines with completion sentinels, not babysitting. Each step handles its own failure; the chain does not abort on a step's exit code. A watcher wakes the orchestrator on the sentinel.

**O9.** `[proven]` Snapshot deltas every cycle: VCS log across repos, job statuses, row/artifact counts. Cheap what-changed detection catches silent failures within one cycle of their occurrence.

**O10.** `[proven]` Orchestrate by desired state, not command streams. The coordinator publishes authoritative state; workers reconcile toward it idempotently. A missed or repeated message then converges to the same result, making re-dispatch safe by construction.

**O11.** `[strong]` Version the coordinator↔worker protocol and refuse mismatches loudly. Components evolve at different speeds; incompatibility must fail closed, not garble.

**O12.** `[strong]` Give every task a generation counter. Re-dispatch bumps the generation; late output from a stale attempt is recognizably stale and discarded, never merged.

**O13.** `[strong]` Never throttle an explicit human command. Automatic retries get padded backoff; operator-initiated actions execute immediately — the operator never waits on their own action.

**O14.** `[proven]` Audit time-use: every cycle shows either a completed item or a documented blocker plus a productive fallback. This rule is what caps rabbit holes (see L9) — enforce it mechanically, not by mood.

**O15.** `[strong]` Singleton the coordinator: acquire a lock or kill stale instances of yourself at startup, so a zombie can't hold ports, IDs, or write access to shared memory.

---

## Part II — Agent Assignment

**A1.** `[proven]` Decompose by epistemic role, not just by task:
- **Researcher** — reads, analyzes, reports; never writes shared state.
- **Developer** — builds in an isolated workspace; never touches the main line.
- **Auditor/Adversary** — tries to falsify claims and review diffs; severity-ranks findings.
- **Guardian** — adversarially reviews protocols and specs *before* they execute.
- **Orchestrator** — sole writer of shared memory, sole executor of mutations, sole merger.
- **Human owner** — above all of it; signs off on irreversibles.

**A2.** `[strong]` Enforce roles structurally, not by promise: capability-scoped tool grants (read-only planners/reviewers; tiered manifests like read_only / auto_apply / elevated). In the observed record, the only role violation came from the harness itself, not a disobedient agent — structure catches what instructions can't.

**A3.** `[proven]` Subagents get fresh, curated context — never inherited session history, never "go read the plan yourself." The dispatcher provides full task text plus exactly the needed background. This focuses the agent and keeps the dispatcher's own context clean for coordination.

**A4.** `[proven]` One writer per resource. Shared memory has exactly one writing role; every agent owns exactly its own report file. The one concurrency accident on record was a coordinator clobbering its own concurrent edit — caught only by read-back after write.

**A5.** `[strong]` Tier model capability to task complexity: mechanical well-specified tasks → cheap fast model; integration and judgment → standard; architecture, design, review → the most capable. On BLOCKED, escalate deliberately: add context first, then a stronger model, then split the task, then ask the human. Never force the same model to blind-retry unchanged.

**A6.** `[strong]` Structured status protocol for every agent: `DONE` / `DONE_WITH_CONCERNS` / `NEEDS_CONTEXT` / `BLOCKED`. Bad work is worse than no work; an agent that flags a blocker beats one that improvises.

**A7.** `[proven]` Do not trust the report — verify the artifact. "Agent says success" is a claim; the diff, the test run, and the deployed behavior are evidence. Treat suspiciously fast completions as probably incomplete.

**A8.** `[strong]` Two-stage review in fixed order: spec compliance first (nothing missing, nothing extra), then code quality. Loop each stage until approved. Quality-reviewing code that fails spec wastes the review.

**A9.** `[proven]` Adversaries falsify, not confirm — and their sharpest tool is differential testing: re-derive claims independently, hand-compute examples, and build deliberately broken counter-implementations to prove the real one diverges from every wrong variant. A good adversary can predict a production failure before it happens; treat their findings as executable runbooks.

**A10.** `[strong]` Calibrate audits: report what survived scrutiny alongside what failed. An auditor that only produces negatives trains the orchestrator to discount it.

**A11.** `[proven]` Developer agents work in isolated worktrees/sandboxes; the main line is merge-only; merges happen only after review. Every agent verifies it is on its own branch in its own workspace before any commit — the harness itself may have moved it.

**A12.** `[strong]` Agents need liveness protocols just like jobs: heartbeats and incremental artifact flushes. N minutes without a flushed artifact = suspect. On record: two agents died silently at a context-window boundary and were discovered 18 hours later only by their missing deliverables.

**A13.** `[strong]` Dispatch with loss-mitigation built in: every deliverable must be resumable by someone else (files on disk, not in-context state). On agent loss, recover from artifacts; do not restart completed work.

**A14.** `[proven]` Execute the requested scope exactly; unrequested "improvements" are defects. The worst class is the band-aid feature that masks the real problem (smoothing bolted on to hide a bug "prevents seeing the actual problems"). Transcript evidence: ~150 scope-restriction commands and ~80 out-of-scope corrections from the human — each one a reversal the agent forced. Do the boring mandated steps (push, format, verify) every time without being re-told.

---

## Part III — Loop Engineering

**L1.** `[proven]` Wake mechanisms are load-bearing infrastructure. Ship them as script files, never inline command strings — quoting/escaping mangled an inline scheduler at launch and produced phantom ticks.

**L2.** `[proven]` Prefer event-driven wakes (sentinels, watchers) over time-driven ticks once real signals exist. Time ticks are the fallback heartbeat, not the primary signal; retire the clock when the event source is armed.

**L3.** `[strong]` One loop instance per purpose, one unique sentinel per loop. Check for an existing loop before starting another. Kill the loop the moment its purpose resolves.

**L4.** `[strong]` Close the loop on ticks: the scheduler must notice a tick that produced no cycle (acknowledgment tracking). Open-loop schedulers accumulate phantom ticks and silent stalls.

**L5.** `[proven]` The cycle is a fixed liturgy: read reports → verify heartbeats → snapshot deltas → heal failures → merge/execute reviewed work → refill the queue → write the cycle report. Healing precedes new work; reporting is never skipped.

**L6.** `[proven]` Every background job gets a watcher or notify pattern at launch — never launch-and-hope. On failure: diagnose from the log, fix, relaunch, record.

**L7.** `[proven]` Liveness of compute jobs = resource delta (CPU-seconds, memory growth), never log output or row counts. Many workloads are silent by design until completion. The costliest self-inflicted incident on record was killing a healthy multi-hour job because its log looked frozen.

**L8.** `[proven]` Loop recovery is a defined procedure, not improvisation: read the memory file top to bottom, check the VCS log, check unprocessed reports, check running jobs — then resume. Trust the memory file over intuition; do not restart completed work.

**L9.** `[proven]` Cap rabbit holes by rule: N failed attempts or M hours → park it, document the honest gap, queue the investigation as off-loop work. Twin rule for debugging: 3+ failed fixes means the architecture is wrong — stop fixing symptoms.

**L10.** `[proven]` Loops report to two audiences every cycle — machine-readable memory and the human — from one single-sourced report. The copies diverging is itself a bug (it happened; the divergence had to be corrected by later forensics).

**L11.** `[strong]` Wrap-up is part of the loop: a closing sweep (connectors green, gates passing, integrity verified) plus a before/after ledger and an explicit handoff of leftovers. Loops that just stop leak state.

**L12.** `[strong]` Externalize workflow state as visible labels/states on the artifacts themselves (e.g., a state machine of labels on a PR/task). A killed agent session then loses nothing — any successor resumes from the artifact. Clean stale workspaces and residual states at startup and shutdown.

---

## Part IV — Context Integrity

### Loss

**C1.** `[proven]` Externalize state to disk the moment it matters; assume the context window can vanish at any time. Everything that survived session death on record survived because it was a file.

**C2.** `[strong]` Compact at phase boundaries (research→plan, plan→implement, after a failed approach), never mid-implementation — that loses variable names, file paths, and partial state at the worst time.

**C3.** `[proven]` Write the distilled artifact (plan, spec, findings) to a file *before* compaction, then let the file be the memory. What survives: files, task lists, VCS state. What dies: reasoning, read contents, conversational nuance.

### Pollution

**C4.** `[proven]` Budget context like memory: compressed reads, deduplication, lazy-load guidance by trigger instead of always-on injection, and measure the savings (a compression layer sustained ~60% token reduction in production use).

**C5.** `[strong]` Audit the guidance stack for duplication — injected context is a tax on every turn. The observed stack carried a fully duplicated skill tree and six overlapping variants of one methodology with two contradictory ceremonies. One canonical source per rule; everything else links.

**C6.** `[proven]` Construct subagent context deliberately; never let it inherit and never make it scavenge (same mechanism as A3, stated for the context ledger: curation keeps both the agent and the dispatcher clean).

### Contamination (embargoed information)

**C7.** `[proven]` Embargo by immutable property (date, content hash), never by mutable membership (ID lists, flags, manifests). The worst leak on record: an ID-list guard plus a never-committed manifest silently let ~800 embargoed records into production training. The cutoff property is the contract.

**C8.** `[proven]` Enforce embargoes once, at the lowest data layer, with self-managing lifecycle (auto-expires when the embargo lifts) — not per-consumer. Every consumer-level guard is one forgotten toggle away from a leak; one such toggle shipped.

**C9.** `[proven]` When a leak-class bug is found, fix every path that shares the flaw. On record: the same guard bug was fixed in one pipeline with an explicit warning comment, and lived on unfixed in a sibling path for months.

**C10.** `[strong]` Precondition diagnostics leak outcome information. Freeze criteria/protocols before the first registered quantity is computed; a "quick check" of the metric is already a look. Amendment windows close at first computation.

**C11.** `[strong]` Config/schema skew between cached artifacts and live code is a leak-shaped bug: fingerprint config and schema into every cached artifact and refuse mismatches. A stale cache once served an old model against a new schema and crashed production on the first tick — exactly as the adversary predicted.

**C12.** `[strong]` Secrets are context too: vault them, inject at execution time, redact on ingestion. Never leave live tokens in agent-readable config files (found twice in the audited setup); block agents from reading secret-shaped files by default.

### Stale replay

**C13.** `[proven]` Historical context is not live instructions. Mark every re-injected summary as HISTORICAL — a model once re-executed a stale command after compaction and duplicated a batch of external side effects (issues, branches, tasks).

**C14.** `[proven]` On resume, reconcile remembered state against ground truth (VCS log, DB counts, running jobs) before acting. Memory tells you where to look; reality tells you what's true.

### Fabricated stand-ins

**C15.** `[proven]` Real data only outside test boundaries — no mock, placeholder, hardcoded, or dummy values in production paths, ever (NOMOCK). Test doubles stay quarantined in tests. The transcript record shows two recurring flavors: hardcoded content baked in instead of wiring real sources (later requiring full "inventory the hardcoding" cleanups), and test dummies bleeding toward real code paths. Fabricated stand-ins are contamination: they make the system *look* wired while measuring nothing.

---

## Part V — Error Handling & Self-Healing

**E1.** `[proven]` Root cause before fixes — no exceptions, especially under time pressure. Read the actual error; reproduce; check recent changes; instrument component boundaries; then form one hypothesis and test it minimally. Systematic debugging measured ~4–8× faster than guess-and-check thrashing.

**E2.** `[proven]` Exit codes and logs are testimony, not evidence — validate the observer before trusting the observation. On record: a wrapper pattern printed *empty* exit codes for a process that verifiably failed, and the OS logged nothing for native crashes; both had to be proven before diagnosis could start. Instrument first; "the observability fixes convert any recurrence into a solved case in minutes."

**E3.** `[proven]` Never kill a process without a diagnosed fault and a written justification recorded *before* the kill. Both unjustified kills on record destroyed healthy work; the in-the-moment rationale "did not survive review."

**E4.** `[proven]` Destructive operations name their exact targets — never time-window or wildcard sweeps — and carry a rollback plan and a pre-surgery snapshot. A cleanup keyed on a time window once swept an entire class of artifacts instead of the intended few.

**E5.** `[proven]` Contain failures at the smallest boundary: one task's crash must never kill the loop (panic containment per cycle; chains that survive step failures; heal-and-retry-once for transient integrity violations).

**E6.** `[proven]` Guard against phantom-empty reads: after any connection or tool error, distrust cheap "empty" answers — a dead client library returning empty state nearly triggered mass destructive convergence. An empty result after an error is a prompt to re-verify the instrument, not a fact about the world.

**E7.** `[proven]` Never broadcast partial state. Hold visibility until loading/replay completes, and mark loading states explicitly — a coordinator that once published a half-loaded book made every worker churn simultaneously.

**E8.** `[strong]` Debounce alarms and kill-switches: trip only on N consecutive breaches against a *persisted* baseline (high-water mark), so a single bad read can never fire an irreversible stop.

**E9.** `[strong]` Incremental persistence: a 97%-complete job must not leave zero artifacts. Long tasks checkpoint as they go — it saves the compute and it leaves forensic evidence when they die.

**E10.** `[proven]` Environment pressure is a first-class failure cause: system-wide memory/commit pressure killed a compute job silently while every smaller sibling survived (more exposure hours = more hazard draws). Monitor the box, provision headroom, and fix the class machine-wide — this applies verbatim to local model inference.

**E11.** `[proven]` Long readers and mutating writers race structurally: snapshot-isolate the reader or freeze mutations for its duration. A mid-task background mutation once invalidated a multi-hour job at its final step.

**E12.** `[proven]` Correct the record when a diagnosis was wrong — explicitly, at the site of the wrong statement. A brain that can't overwrite its own false beliefs compounds them.

**E13.** `[proven]` On contradiction between frozen instructions, HALT and escalate — don't improvise a resolution. Picking an interpretation unilaterally converts a documentation bug into an operational one.

**E14.** `[strong]` Bulk-operation definitions state inclusion evidence, not just exclusions. An exclusion-defined cleanup would have hit ~48× the correct population; requiring positive evidence per target shrank it to exactly right.

**E15.** `[proven]` Identity keys must be deterministic and content-derived; never build identity from process-local state (per-process hash salting, timestamps, counters). Nondeterministic keys silently minted up to 5 duplicate records per real entity and manufactured a fake performance edge that took months to detect (see V11).

**E16.** `[proven]` Codify per-platform tool hazards as defaults, not tribal knowledge. Observed class: shell text-manipulation corrupting file encodings in bulk (use a proper scripting runtime for file edits), NaN vs JSON null at DB boundaries, one orphaned row poisoning an entire batch insert, removing a workspace from inside it failing silently (leave first, then remove). Each cost real hours; each is a one-line rule in the tool layer.

**E17.** `[proven]` Migrations and seed scripts treat live data as sacred: additive by default, never clobber, snapshot before running, and never run a "reseed defaults" path against a store that has accumulated real user state. The worst destructive incident in the transcript record was a seed flow silently wiping live user-created records during a mock-to-database migration — recovery required mining session logs as the backup of record.

---

## Part VI — Verification & Epistemics

**V1.** `[proven]` No completion claims without fresh verification evidence — run the command, read the output, then speak. Applies to every paraphrase of "done." Confidence is not evidence; a previous run is not evidence; an agent's report is not evidence. Empirical weight: in a 315-transcript corpus, hedged "should work / should now work" claims (~530 instances) were the single most frequent trigger of human correction, and the most common human intervention overall was doing the agent's verification for it. Enforcing this one principle removes the largest observed share of corrections.

**V2.** `[strong]` Tests are the spec: no production code without a failing test you watched fail. A test that passes immediately proves nothing. Untested load-bearing assumptions once forced an entire shipped subsystem to be gutted; assumption ledgers + runtime gates now enforce "test the assumption that licenses this code path, then proceed."

**V3.** `[proven]` Verify by invariant arithmetic where possible: design operations so correct behavior has a predictable number attached (row-count ceilings, conservation checks), then match it exactly. The strongest fix-verifications on record were exact ceiling matches.

**V4.** `[proven]` Verify in production reality, not just locally: authenticated end-to-end probes after deploy; commit standing probes; remove probe credentials after. Probes themselves have bugs — verify the probe the first time too.

**V5.** `[proven]` Pre-register protocols for irreversible or statistical decisions: freeze the decision rule (in VCS, hash-pinned) before any result exists. A well-designed protocol will sometimes abort itself at its own precondition — that is the system working, not failing.

**V6.** `[proven]` Adversarially review the protocol itself before it runs (guardian role). Findings format: quoted text → concrete exploit → exact replacement wording. Observed exploit classes: amendment windows that stay open after information leaks, missing re-seal steps, tolerances that are impossible in floating point (a void-at-will reset button), pins by reference that rot.

**V7.** `[proven]` Pre-commit interpretations for every outcome and publish unflattering results at equal prominence. Corrections should be accepted in the conservative direction as readily as the favorable one. A brain that shades its own results cannot learn.

**V8.** `[strong]` Budget looks at held-out truth: a multiplicity ledger where every peek is spent, counted, and never re-rolled. Evals are looks; unlimited re-runs overfit the harness to the benchmark.

**V9.** `[strong]` Distinguish claim classes explicitly: exploratory vs registered; selection-exposed vs confirmatory; and scope every claim to its regime (a result that came from a specific training/config regime is a claim about that regime, not the architecture).

**V10.** `[strong]` Verification is layered — mechanical gates catch instantly (build/type/lint/test on every change, confirmed before "done"), comprehensive suites verify at milestones, adversaries falsify high-stakes claims, invariant arithmetic proves. Different layers catch different lies.

**V11.** `[proven]` Calibrate the measuring instrument before believing any measurement — and treat improbably good results as bugs until falsified. Three convergent implementations: placebo baselines must score exactly zero through the identical pipeline; finalists must reproduce on a disjoint seed range; and the one time a metric looked too good, it *was* the bug (E15). Route outlier successes to an adversary before celebrating them.

**V12.** `[strong]` Where hard sealing is impractical, make peeking auditable instead of impossible: an append-only ledger of every touch of held-out data turns silent overfitting into visible evidence, forever.

**V13.** `[proven]` Epistemic speech discipline: state guesses as guesses, never as facts — and never agree performatively. The transcript signature of the failure: "I stated it as fact instead of a guess — that's my mistake," and a 10:1 ratio of cheap agreement ("you're right": 131) to actual ownership ("my mistake": 13). Corollary: verify review feedback before implementing it — sycophantic agreement followed by blind implementation of wrong feedback is a documented failure chain.

---

## Part VII — Governance & Enforcement

**G1.** `[proven]` The enforcement ladder: prompt guidance → injected context → automatic transformation → hard block → structurally impossible. Push every rule as far down the ladder as it can go. "If it's enforceable with regex/validation, automate it — save documentation for judgment calls." Asking an agent "are you sure?" fails (it always says yes); forcing it to *produce facts* (importers, schemas, targets, a verbatim instruction quote) changes behavior.

**G2.** `[proven]` Deployed enforcement ≠ designed enforcement. Audit the wiring: the audited setup had its strongest gates written but unwired, one gate dead-coded on the host OS, and discipline silently resting on the model choosing to load the right guidance.

**G3.** `[proven]` Enforcement code is code: it needs tests, health checks, and integrity verification. A corrupted hook file sat broken and unnoticed in the audited setup. A platform whose guardrails can silently rot has no guardrails.

**G4.** `[inferred]` Forbid gate-gaming mechanically: no verification bypass flags, no editing linter/CI configs to force green, no weakening tests to pass them. Every one of these blockers exists because an agent did exactly that.

**G5.** `[strong]` Instruction priority is explicit and total: owner > project rules > platform guidance > defaults. Without a declared override chain, conflicting guidance resolves by load-order luck.

**G6.** `[proven]` High-stakes actions take three parties: proposer, adversarial reviewer, and executing authority — with the human as final sign-off for irreversibles. (Mutations: developer proposes dry-run → auditor reviews → orchestrator executes, serialized. Protocols: author → guardian countersign → owner sign-off → execute.)

**G7.** `[strong]` Pin by value, not by reference — referenced docs rot out from under the pin. Later documents supersede explicitly; silent contradiction between two live documents is a defect (and once forced a full HALT at execution time).

**G8.** `[strong]` Self-modification follows preflight → hash-verified backup → apply → verify. Any system that rewrites its own configs keeps a receipt chain that can prove and undo every change.

**G9.** `[strong]` A summary of a procedure is not the procedure. Documented bug: an agent followed a skill's one-line *description* and skipped one of its two mandatory review steps. Write descriptions that trigger loading the full text, not ones that read like an executable digest — and make multi-step procedures enumerate their steps as checkable items so a skipped step is visible.

---

## Part VIII — Memory & Learning (the self-improvement protocol)

**M1.** `[proven]` One plain-text, human-readable memory file per mission; single writer; append-only audit semantics. Plain text beats clever storage because every agent and every human can read it under any failure mode.

**M2.** `[strong]` Structure memory as an event-sourced journal (protocol, connectors, queue, in-flight, decisions, cycle reports) and *generate* the current-state view from it. The observed flat file's failure mode was chronology drift — a cold reader could misread which state was current.

**M3.** `[proven]` Record incidents in a fixed format, including self-inflicted ones, with honest damage assessment. Honesty about self-inflicted damage is what makes the record trainable:

```
INCIDENT #N — <timestamp> <mission>
WHAT:       what happened, observably
DAMAGE:     honest cost, including "self-inflicted"
ROOT CAUSE: mechanism, not blame
LESSON:     one imperative rule (candidate principle)
PROMOTION:  memory → rule → gate → structural   [current stage]
```

**M4.** `[strong]` Promote lessons up the ladder: incident → memory entry → codified rule → mechanical gate → structural impossibility. Promotion includes **amending the source that misled you** — the observed system once recorded a corrected lesson while leaving the wrong frozen rule in place, and the stale rule remained a trap.

**M5.** `[strong]` Close the learning loop automatically: mine transcripts and session logs for recurring patterns; store candidates as confidence-weighted instincts; re-inject only a bounded number of high-confidence ones at session start (observed working defaults: ≥0.7 confidence, ≤6 injected); recurring confirmation raises confidence, contradiction lowers it; confident + mechanically expressible → promote to a gate (M4).

**M6.** `[proven]` Maintain errata as first-class memory: corrections supersede at the site of the error with a dated note ("correction noted for any future citation") — never silent overwrites, never orphaned wrong values.

**M7.** `[strong]` Instrument the harness and verify the instruments. The audited context-savings tooling was genuinely effective (~60%) while one of its own counters over-reported by ~100×. Telemetry feeding the learning loop must itself be validated, or the brain learns from corrupted reward signals.

**M8.** `[strong]` Memory hygiene is scheduled work: archive per-mission files at wrap-up, separate curated reports from debug residue, expire stale state. Folders that double as audit trails decay into middens without it.

**M9.** `[strong]` Cross-session continuity is a protocol, not an accident: save state before any clear, load on start, then reconcile against ground truth (C14) before acting. Target: a few-hundred-token warm start instead of a tens-of-thousands-token cold rebuild.

---

## Part IX — Failure taxonomy (ranked by observed cost)

1. **Contamination via mutable-membership embargo guards** (ID lists + uncommitted manifests → embargoed data in production training) → C7–C9.
2. **Operator-inflicted damage under uncertainty** (false-positive kills of healthy jobs; over-broad destructive sweeps) → L7, E3, E4. In the observed loop, the orchestrator itself was the single largest source of damage.
3. **Silent infrastructure death** (native aborts under memory pressure with no logs; blind exit codes; agents dying at context boundaries) → E2, E9, E10, A12.
4. **False completion claims** (success reported without evidence; empty exit codes read as success; agent reports taken at face value) → V1, A7, E2. *By frequency this is #1: it triggered more human corrections than any other pattern in the transcript corpus.*
5. **Nondeterministic identity** (process-local hashing minted duplicate entities and a fake performance edge) → E15, V11.
6. **Partial-state broadcast** (half-loaded state published; every consumer churned) → E7; agent analog C1/C2.
7. **Stale replay after compaction** (historical command re-executed; external side effects duplicated) → C13, C14.
8. **Shared-state races** (mutation under a long reader; duplicate-minting re-points; self-clobbered concurrent edits) → O3, A4, E11.
9. **Harness bugs masquerading as agent misbehavior** (runner moved a workspace's branch; inline-command escaping killed a scheduler; corrupted hook; OS-gated dead enforcement) → L1, A11, G2, G3.
10. **Protocol exploits and drift** (amendment-window leaks; missing re-seal; impossible tolerances; by-reference rot; frozen self-contradiction) → V5–V8, G7, E13, M4.
11. **Untested load-bearing assumptions shipped** (an entire subsystem gutted after the fact) → V2, G6.
12. **Context waste** (duplicated guidance trees injected wholesale; six overlapping variants of one methodology) → C4, C5.
13. **Rabbit holes** (hours + repeated attempts without a cap; timeout-bumping as a fake fix) → L9, E1.
14. **Gate gaming** (verification bypass flags; config edits to force green) → G4.
15. **Cache/schema skew** (stale artifact served against new schema — predicted, then observed) → C11.
16. **Secrets in agent-readable plaintext** → C12.
17. **Per-platform tool hazards** (encoding-corrupting bulk edits; NaN/null boundary bugs; one bad row poisoning a batch; workspace removed from inside itself) → E16.
18. **Scope creep and band-aid features** (unrequested additions masking real bugs; ~150 scope-restriction commands, ~80 out-of-scope corrections in the transcript corpus) → A14.
19. **Fabricated stand-ins** (mock/placeholder/hardcoded values in production paths; test dummies bleeding into real code) → C15.
20. **Seed/migration clobber of live data** (a reseed flow wiped live user-created records during a mock-to-database migration) → E17, E4.
21. **Speculation stated as fact + performative agreement** (assume → theorize → get corrected → retract; cheap agreement outnumbering ownership 10:1) → V13, G1.

Ranking note: the list is ordered by observed *cost*; by observed *frequency* the order inverts — unverified completion claims, speculation-as-fact, fix-that-doesn't-fix, and scope creep dominate day-to-day corrections, while contamination and destructive incidents dominate total damage.

---

## Part X — Applying this brain to the platform

### Components ↔ principles

- **Orchestrator kernel** — singleton (O15); publishes desired-state task graph, agents reconcile (O10); versioned protocol (O11); task generations (O12); panic-contained cycles (E5); fixed cycle liturgy (L5).
- **Scheduler / loop engine** — typed queue (O1), 3–5 in flight (O2), serialized mutation lane (O3/O6), sentinel chains (O8), tick acknowledgment (L4), time-use auditor (O14), rabbit-hole caps (L9), externalized workflow states (L12).
- **Agent runtime** — role manifests with structural tool grants (A1/A2); constructed context per task (A3/C6); status protocol (A6); model tiering + escalation (A5); workspace isolation (A11); heartbeats + partial-artifact flushes (A12/E9); resumable deliverables (A13); offline-stub mode so the whole platform runs and tests with no model attached.
- **Verification harness** — mechanical gates on every change (V10/G1); the "done" evidence gate (V1); diff-verification of agent reports (A7); two-stage review (A8); adversary tier with differential testing for high-stakes claims (A9/A10); invariant arithmetic (V3); production probes (V4); instrument calibration with placebos and disjoint seeds (V11).
- **Context subsystem** — budgeted, compressed, deduplicated context (C4/C5); phase-boundary compaction only (C2); externalize-then-compact (C1/C3); property-based embargoes at the data layer (C7/C8); config fingerprints in caches (C11); secret vault + redaction (C12); stale-replay banners and ground-truth reconciliation (C13/C14).
- **Memory & learning ("the brain")** — event-sourced journal + generated views (M1/M2); fixed incident format (M3); promotion ladder with source-amendment (M4); transcript mining → bounded instinct injection (M5); errata (M6); validated telemetry (M7); scheduled hygiene (M8); warm-start continuity (M9). **Seed it with this file.**
- **Model layer** — local router with capability tiers; cheap-model default with deliberate escalation (A5); best-of-n plus adversarial verification where correctness outranks latency; eval-gated promotion of any router/prompt change, with evals treated as budgeted looks (V5/V8).
- **Enforcement engine** — hard blocks for destructive and gate-gaming actions (G1/G4); fact-forcing gates before risky operations (G1); wiring audits and self-tests of the enforcement layer itself (G2/G3); receipt-chained self-modification (G8).
- **Governance** — registration/freeze for irreversibles (V5); guardian review of protocol changes (V6); three-party execution (G6); pre-committed interpretations and equal-prominence publication of failures (V7).

### Day-one hygiene defaults

- Hard-block: verification bypass flags, linter/CI-config edits to force green, destructive commands without enumerated targets + rollback plan, seed/migration runs against live stores without a snapshot, reads of secret-shaped files, commits to the main line by non-orchestrator roles.
- Require: fresh verification evidence before any "done"; dry-run + review before any shared-state mutation; incident record before any process kill; branch/workspace verification before any commit; real data wiring (no mock/placeholder values) outside test boundaries; guesses labeled as guesses.
- Wire: heartbeats on every background job and agent; tick acknowledgment in the scheduler; telemetry validation checks; enforcement self-tests in CI.

### Build order (leverage per unit effort)

1. **Memory kernel** (M1–M9) — everything compounds through it; cheapest to build.
2. **Verification harness** (V1, A7, V10) — the biggest quality multiplier per token, model-agnostic; empirically, "prove it ran, with output" removes the largest share of human corrections observed in the entire transcript record.
3. **Scheduler + loop engine** (Part III) — turns a chat tool into an operations system.
4. **Role runtime with structural grants** (A1–A4) — closes the honor-system gap behind most observed near-misses.
5. **Local model serving + router** — last: Parts I–VIII are model-agnostic and already proven; swap the model seam in once the harness deserves it.

### The one-sentence thesis

Frontier-level capability is a property of the *system*, not the model: unlimited local token budget makes verification loops, best-of-n, and adversaries free; total control of context and telemetry makes Parts IV and VIII implementable at the platform layer; and a memory that honestly records, promotes, and enforces its own lessons compounds while a stateless assistant stays flat.

---

## NEW PRINCIPLES (appended by the learning loop)

Founding memory grows here. Generalized principles promoted from this platform's
OWN incidents (per M4/M5): distilled at mission retrospectives, proposed as
amendments with the next free ID in the appropriate series, operator-
countersigned, applied by the amendment mission, and cited like any seed
principle. Confidence starts at `[strong]` (one incident, codified) and is
upgraded to `[proven]` on recurrence; a principle contradicted by later
evidence gets an erratum below, never a silent edit.

**E18.** `[strong]` A config value you never saw take effect is a guess: tools ignore
unknown keys silently. After writing config, verify the OBSERVABLE effect — the bound
address, the loaded model, the actual limit. (incident: platform-build/serving — a
yaml `listen:` key was really a CLI flag; the server sat exposed on 0.0.0.0, 2026-07-10)

**E19.** `[strong]` Classify failures before counting them: an infrastructure failure
recorded as a work failure poisons the failure memory with false negatives and burns
retry budget on attempts the worker never got to make. Refund infra-caused attempts;
heal the platform; retry bounded. (incident: platform-build/conductor — API 400s
consumed all task attempts in seconds, 2026-07-10)

**V14.** `[strong]` Smoke tests must use production-shaped payloads. A serving stack
that answers a 10-token hello can be 100% unusable for its real workload; probe with
the size, shape, and auth of real traffic before declaring it up. (incident:
platform-build/serving — context split across parallel slots passed hello, rejected
every agent session, 2026-07-10)

**G10.** `[strong]` Check the pulse of every third-party dependency at pin time:
archived repo, last release date, platform-specific builds present, remote file
layout matching your fetch pattern. Two minutes of checking beats shipping a
dependency discontinued months earlier. (incident: platform-build/ide — an extension
was installed fresh two months after its project shut down; a model download pattern
matched zero files, 2026-07-10)

---

## ERRATA (append-only; per M6)

Corrections and amendments to the principles above. Each entry: date, principle
ID, what changed, citing incident. Never edit the principle text in place.

- *(none yet)*
