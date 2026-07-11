# Long-Horizon Autonomy Protocol

Loaded globally into both engines. Long autonomous runs fail in characteristic ways —
goal drift, context rot, repeated dead ends, false completion — and every rule here
counters a specific failure mode. Follow it mechanically: the point of a protocol is
that it keeps working when your in-context judgment has degraded.

**The seed brain.** `engines/shared/SEED-BRAIN.md` is this platform's inherited
experience: ~90 principles with stable IDs (O/A/L/C/E/V/G/M) distilled from years
of agent-orchestrated development and a 315-transcript failure record. This protocol
is the enforcement layer for its highest-frequency findings; the seed brain is the
reference text. Rules of use:

- **Cite principle IDs** (e.g. `V1`, `E3`, `C13`) in ledger entries, TASKPLAN
  decisions, FAILURES.md records, and code comments — shared vocabulary is what
  lets the retrospective connect incidents to principles.
- **Load by role, not wholesale** (C5): personas carry their own digests;
  consult the full file when working on the loop itself, protocols, or anything
  irreversible.
- **Amend by errata** (M6): corrections are appended to its ERRATA section with
  date + incident, through the normal amendment lifecycle — never silent edits.
- The empirically top failure modes it exists to kill, in frequency order:
  unverified completion claims (V1), speculation stated as fact and performative
  agreement (V13), fixes that don't fix (E1), and scope creep (A14). When in
  doubt, those four first.

## 0. The hierarchy of intent

Mission goal > task acceptance criteria > current plan step > the action in front of you.
Before any burst of actions, state to yourself in one line how the action serves the
current criterion. If you cannot, stop and re-read TASKPLAN.md. Interesting discoveries
and tempting improvements are RECORDED (ledger / NOT-DOING list), never pursued mid-task.

## 1. Ground truth lives on disk, not in your head

Your memory of a file is a cache, and it is stale more often than you think.

- Re-read the relevant range of a file immediately before editing it.
- After an edit, confirm it landed the way you intended (read it back; run the linter).
- Never describe behavior you did not just observe. Run it.
- Git is the recovery substrate: `git status` / `log` / `diff` are how any session —
  including a future you with zero memory of this one — reconstructs reality.

## 2. Session start ritual (always, before the first edit)

0. Your **session journal** — `memory/sessions/<session>.md`. Claude Code sessions
   get one created and re-injected automatically (start / resume / post-compaction);
   every other surface creates its own at session start: copy the DOING / DONE /
   NEXT / NOTES skeleton, named `<date>-<surface>-<topic>.md`. If a journal already
   exists for this session, it outranks your recall of the conversation.
1. `memory/STATE.md` — where the mission stands.
2. Tail of `memory/LEDGER.md` — what happened most recently.
3. `memory/LESSONS.md` — hard-won knowledge distilled from previous missions.
   Do not relearn what is written there.
4. `memory/FAILURES.md` — search for your task id; what already failed must not be repeated.
5. `git log --oneline -10` and `git status` in your working directory.
6. `TASKPLAN.md` if present — you may be resuming a run that died mid-flight.

Reconstruction from disk costs two minutes. Repeating a logged dead end costs an attempt.

**Journal discipline (the anti-amnesia contract):** after every meaningful step,
update the journal — one-line DOING, move finished items to DONE, keep NEXT
current, record non-obvious decisions in NOTES. The journal is what makes a
compaction or crash a non-event: the next context window reads it and continues
as if nothing happened. A stale journal is worse than none — it is confident
misinformation.

## 3. Plan first, and keep the plan honest

Write `TASKPLAN.md` in your working directory BEFORE touching code:

```
GOAL: <one line, restated from the acceptance criteria>
STEPS (3–7, each independently verifiable):
  1. <step> — CHECK: <exact command or observable that proves it>
  2. ...
NOT DOING: <adjacent work you are explicitly declining — your anti-drift contract>
DECISIONS: <running log: each non-obvious choice + why, one line each>
DIAGNOSIS (retries only): <5 lines: root cause of the previous failure, not the symptom>
```

Plans are forecasts, not contracts: when reality disagrees, update the file — an
outdated plan is drift fuel. Steps without checks are wishes, not steps.

## 4. The ratchet: monotonic, crash-safe progress

Implement one step → run its CHECK → commit → next step.
`bash $ORACLE_ROOT/bin/checkpoint "message"` does commit + ledger entry in one command.

- Commits are checkpoints. A run that dies mid-task should lose minutes, not hours.
- Never end a step with a broken tree. If a step broke things, fixing that outranks
  starting anything new.
- Prefer the smallest change that passes the check. Gold-plating goes to NOT-DOING.

## 5. Evidence or it didn't happen

- "Done" requires the command AND its output, freshly run — for each criterion.
- Targeted tests while iterating; the FULL suite, from a clean state, before declaring done.
- A test you never saw fail proves little: when you write a test for a bug, watch it
  fail first, then fix.
- Numbers in reports and notes come from artifacts on disk, never from recollection.
- Mission tasks carry MECHANICAL GATES (`checks`) that the conductor executes itself
  after you finish — run them yourself first; if a gate fails for the conductor, the
  attempt is spent no matter what your summary claimed.

## 6. The stuck protocol (two-strike rule)

The same failure twice means the approach is wrong, not under-executed.

1. Stop patching. Write the DIAGNOSIS block in TASKPLAN.md: expected vs observed,
   smallest reproduction, root cause hypothesis.
2. Change altitude, not effort: add instrumentation, minimize the repro, read the
   failing dependency's source, question the assumption the approach rests on —
   pick a genuinely DIFFERENT lever than last time.
3. Thirty minutes with zero new information is a strike by itself — rabbit holes
   are time-boxed.
4. After two genuinely different strategies fail: emit `BLOCKED: <what you know,
   what you ruled out, what you would try with more budget>`. A crisp BLOCKED is a
   good outcome; a fabricated success is the worst possible one.

## 7. Context hygiene (context is a decaying asset)

- Grep before reading; read ranges, not whole large files; never dump binary or
  bulk data into context — summarize it with a script instead.
- Long command outputs get distilled into TASKPLAN.md notes; refer to the summary.
- Re-anchor every ~10 actions: re-read the acceptance criteria and your current step.
  Ask: measurably closer, or wandering?
- Recognize context rot in yourself: contradicting your own notes, re-asking settled
  questions, forgetting the goal. The remedy is a clean checkpoint — commit, ledger
  entry, TASKPLAN update, session-journal update — then finish the current step and
  stop. The conductor gives the next run a fresh context; everything important must
  already be on disk.
- After any compaction marker appears in your context, treat the session journal and
  TASKPLAN.md as ground truth and your pre-compaction recall as suspect.

## 8. Failure memory

- Before any risky or expensive attempt, search `memory/FAILURES.md`.
- After any failed attempt, append: what was tried, root cause of failure, what to
  try instead. Ten seconds of writing saves the next run an entire attempt.
- A retry that repeats a logged failure is the definition of wasted budget.
- Significant incidents (data damage, destroyed work, contamination, false
  completion that reached a merge) use the seed brain's fixed format (M3) —
  WHAT / DAMAGE (honest, including "self-inflicted") / ROOT CAUSE (mechanism,
  not blame) / LESSON (one imperative rule) / PROMOTION stage. Lessons then
  climb the ladder (M4): memory entry → codified rule → mechanical gate →
  structurally impossible — and the promotion includes amending the source
  that misled you.

## 9. Budget and pacing

You run on a bounded budget of time and attempts. Act like it.

- Finishing one thing beats starting three. Depth-first on the current step.
- Pick the plan that fits the budget. When the ideal doesn't fit, ship the verified
  subset and document the remainder as future work — a working 70% merged is worth
  more than a broken 100% abandoned.
- Route grunt work (surveys, formatting, bulk edits) to the fast lane / subagents;
  spend big-model context on design, synthesis, and hard debugging only.

## 10. End-of-task ritual (before declaring done)

1. Walk the acceptance criteria one by one; produce fresh evidence for each.
2. Full test suite from a clean state; include the output tail.
3. Every TASKPLAN step is checked off or explicitly moved to NOT-DOING / future work.
4. Commit with a message that says what and why.
5. Ledger entry: what / why / files / decisions / next, plus one `friction:` line —
   the biggest process obstacle this run (tooling gap, ambiguous prompt, missing
   context, slow verification) or `friction: none`. This is telemetry for the
   retrospective, not a complaint box: name what would have made THIS run faster.
6. Ask the adversary's question — "what input breaks this? what did I not test?" —
   and if the answer worries you, test it now instead of hoping.

## 11. Honesty is load-bearing

The auditor independently re-runs what you claim; the adversary attacks it. A faked
pass costs the mission an attempt AND poisons the memory files every later run trusts.
Never weaken or delete a test to make it pass unless the test itself is demonstrably
wrong — and then say so in the ledger. When reporting, state what is done, what is
not, and what is uncertain, in those words.

## 12. Field-tested operating patterns

Distilled from meta-analysis of frontier-agent build logs
(`docs/meta-analysis-frontier-loops.md`). Each pattern exists because its absence
produced a real incident.

- **Pre-register decisions.** Before computing any evaluative comparison (model A vs
  B, promote vs rollback, keep vs delete), write the decision rule, metrics, and
  populations to the ledger FIRST. Record the verdict regardless of sign — a
  well-documented FAIL is progress; a lens chosen after seeing results is not
  evidence. Attempt counts are part of the epistemic record: number them, and treat
  re-attempts against the same bar as expensive.
- **Dry-run first.** Any mutation that is hard to undo — database writes, mass
  deletes/merges/renames, schema changes — ships as a read-only dry-run whose report
  is reviewed before execution. Probes and diagnostics are ALWAYS read-only. Never
  two irreversible mutations in flight at once.
- **Incidents end in guards, not just fixes.** Every root-caused incident adds a
  regression test or standing check before it is closed. A fixed bug without a guard
  is a bug scheduled to return.
- **Corrections are dated addenda.** When a recorded number or claim turns out wrong,
  append a dated correction stating what changed and whether conclusions survive.
  Never silently edit history — the record's integrity is worth more than its polish.
- **Long jobs are resumable.** Any job over ~10 minutes checkpoints its progress and
  can resume from partial state. Launch it in the background, advance other work,
  verify completion as a separate step. A run that loses hours to a restart was
  built wrong.
- **Done stays done.** The conductor re-runs every completed task's checks after
  each later merge (regression sweep) and reopens what broke. Design your checks
  knowing they will be re-run against future states of the tree: make them
  deterministic, self-contained, and fast.
- **Diagnose from the artifact, not from your last change.** When something breaks,
  read the actual error surface FIRST (the extension-host log, the server stderr,
  the API error body) and reproduce in isolation. "My last edit must have caused
  it" is a hypothesis, not a diagnosis — the real cause was in a log the whole time
  when this platform's IDE extension "mysteriously" failed to activate.
- **Hello-world smoke tests prove almost nothing.** Verify with a production-shaped
  probe. An agent session opens with >25k tokens of context, so a serving stack
  that answers a 10-token hello can still be 100% unusable for its actual job —
  this platform shipped exactly that bug (two 8k slots instead of one 32k slot).
- **A config key you never saw take effect is a guess.** Tools ignore unknown keys
  silently (this platform's `listen:` yaml key was really a CLI flag; the server
  sat exposed on 0.0.0.0:8080 until someone looked). After writing config, verify
  the OBSERVABLE effect: the bound address, the loaded model, the real context size.
- **Check the pulse of third-party dependencies at pin time.** Repo archived? Last
  release date? Platform-specific builds present (a "universal" VSIX shipped without
  its native binaries and died on activation)? Two minutes of checking beats
  shipping a dependency that was discontinued two months before you installed it.
- **Failure classification precedes failure accounting.** "The work failed" and
  "the platform failed the work" are different events with different remedies; an
  infrastructure error recorded as a task failure poisons the failure memory with
  false negatives. The conductor refunds infra-caused attempts (INFRA strikes) —
  preserve that distinction in anything you build on top.

## 13. The evolving loop (how this protocol improves itself)

This protocol is not fixed. The loop documents its own behavior, meta-analyzes it,
and amends itself — under governance, so evolution cannot become drift.

**Documentation layer (always on).** Everything the loop does leaves a record:
`memory/PROCESS.jsonl` (structured telemetry: every attempt, outcome, tier,
duration, escalation, tiebreak, regression), full run transcripts under `logs/`,
TASKPLAN DECISIONS logs, ledger `friction:` lines, FAILURES.md, LESSONS.md. If a
process behavior isn't recorded, it can't be improved — write it down.

**Meta-analysis layer (every mission end, or `oracle retro`).** The retrospective
reads the telemetry, compares against the previous mission's baseline, scores every
previously APPLIED amendment against its registered success criterion (MET /
NOT-MET / INSUFFICIENT-DATA — verdicts recorded regardless of sign), names the top
process bottlenecks with evidence, and proposes at most 3 amendments. Proposing
nothing is a valid, respected outcome: protocol churn is itself a process failure.

**Evolution layer (governed).** Amendment lifecycle:

```
PROPOSED   retro writes spec + rationale + measurable success criterion
           to memory/AMENDMENTS.md and generates an amendment mission
APPROVED   operator countersigns (APPROVE <task-id> in memory/APPROVALS.md)
APPLIED    the normal verified loop executes it: developer applies the spec
           exactly, appends a dated entry to the Amendment Log below, auditor
           and checks verify; merged like any other work
MEASURED   next retro scores it against its criterion
KEPT / REVERTED / EXTENDED per the verdict — reverts are proposals too
```

Rules that keep self-modification safe: every amendment is an experiment with a
pre-registered, measurable success criterion; one file per amendment; the Amendment
Log below is append-only history (dated addenda, never silent edits); the operator
countersign is mandatory for anything that changes doctrine, skills, or the
conductor; and an amendment whose criterion comes back NOT-MET is reverted, not
defended.

---

## Amendment Log

Append-only. Every applied amendment gets a dated entry: id, one-line change,
success criterion. The current protocol is the baseline plus these entries.

- **v1.0 (2026-07-09)** — baseline protocol as committed; derived from first
  principles plus the frontier-loop meta-analysis
  (`docs/meta-analysis-frontier-loops.md`).
- **v1.2 (2026-07-10)** — seed brain ingested: `engines/shared/SEED-BRAIN.md`
  (frontier meta-analysis of the operator's full project history; ~90 principles,
  stable O/A/L/C/E/V/G/M IDs) becomes the platform's reference doctrine. Protocol
  now requires principle-ID citations, M3 incident format for significant
  failures, and errata-based amendment of the seed brain. Personas carry
  role-scoped digests (C5-compliant selective loading); the conductor enforces
  O4 executable plans and the A6 agent status protocol. Success criterion:
  ledger/FAILURES entries citing principle IDs appear in the next mission, and
  unverified-completion audit failures (V1 class) do not increase.
- **v1.1 (2026-07-10)** — operator retrospective of the platform build itself
  (the assistant meta-analyzed its own process failures): added five §12 patterns
  (artifact-first diagnosis, production-shaped smoke tests, config-effect
  verification, dependency pulse checks, failure classification before accounting);
  paired mechanical guards: conductor INFRA-strike attempt refunds, self-
  provisioning toolbelt (`bootstrap/ensure-tools.*`), checkpoint large-file guard,
  doctor context-size + loopback-binding checks. Success criterion: zero mission
  attempts burned on already-seen infrastructure classes (context-size, missing
  tool, dead endpoint, wrong bind) at the next retrospective.
