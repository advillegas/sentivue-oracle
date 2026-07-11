---
name: loop-engineering
description: Design and audit self-governing agent loops - the 7 production patterns (daily triage, PR babysitter, CI sweeper, dependency sweeper, changelog drafter, post-merge cleanup, issue triage), the five building blocks + memory, phased rollout (L1 report -> L2 assisted -> L3 unattended), and the loop-audit/loop-cost/loop-sync CLIs. Use when designing a new mission cadence, auditing loop readiness, or choosing an automation pattern.
---

# Loop Engineering

Reference distilled from the vendored `harness/loop-engineering/vendor` checkout
(pinned; full patterns, failure modes, and primitives matrix live there).
Thesis: **stop prompting agents - design the loop that prompts them.** This is
the same doctrine the conductor implements; use this skill when creating NEW
loops or auditing existing ones.

## The five building blocks + memory

| Primitive | Job | This platform's implementation |
|---|---|---|
| Automations / scheduling | discovery + triage on a cadence | conductor missions, `report_minutes`, background tasks |
| Worktrees | safe parallel execution | `.worktrees/` per task, gitlock-serialized merges |
| Skills | persistent project knowledge | `skills/*/SKILL.md`, synced to both engines |
| Plugins & connectors | reach into real tools | MCP (duckdb/postgres), envoy for controlled net |
| Sub-agents | maker / checker split | developer/auditor/adversary personas + conductor verification stack |
| + Memory / state | durable spine outside any conversation | seed brain (L0), LESSONS (L1), LEDGER/STATE/FAILURES, session journals |

## The 7 production patterns (choose by cadence and blast radius)

1. **Daily triage** (1d-2h, low cost) - survey repo/issues/state, write a report, fix nothing. The safest first loop.
2. **PR babysitter** (5-15m, high cost) - keep a PR merge-ready: comments, conflicts, CI.
3. **CI sweeper** (5-15m, very high cost) - watch CI, diagnose and fix failures.
4. **Dependency sweeper** (6h-1d) - patch-level bumps with full test gates.
5. **Changelog drafter** (1d or on tag, low cost) - draft release notes from the log.
6. **Post-merge cleanup** (1d, off-peak) - dead branches, stale worktrees, TODO sweeps.
7. **Issue triage** (2h-1d, propose-only) - label, deduplicate, propose plans.

## Phased rollout (never skip phases)

- **L1 report-only**: the loop observes and writes reports. Week one is ALWAYS L1.
- **L2 assisted**: the loop fixes narrow, allowlisted classes; human merges.
- **L3 unattended**: full autonomy inside hard gates (this platform's mission loop
  with checks + audit + approval gates for irreversibles).

## CLIs (pinned in .tools/npm; run via `oracle loops ...`)

- `loop-audit .` - Loop Readiness Score with `--suggest` fixes (state, skills,
  budget, constraints, governance scoring).
- `loop-init . --pattern <p> --tool claude` - scaffold a new loop (STATE/LOOP/budget).
- `loop-cost --pattern <p> --level L1` - token spend estimate for a cadence.
- `loop-sync .` - drift detection between STATE.md and LOOP.md.

## Design rules that pair with the seed brain

- Every loop states its budget and kill criteria up front (O14, L9).
- One loop instance per purpose; unique sentinel; kill on purpose-resolved (L3).
- Report to machine memory AND the human from one source (L10).
- Wrap-up sweep is part of the loop; loops that just stop leak state (L11).
- Week-one loops are report-only; verification never comes for free (V1).
