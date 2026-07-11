# LOOP.md — the loops that run this platform

This file documents the self-governing loops (loop-engineering convention;
audit with `oracle loops audit`). The implementation is the conductor
(`conductor/conductor.py`); doctrine is `engines/shared/AUTONOMY.md` and the
seed brain (`engines/shared/SEED-BRAIN.md`).

## Loop inventory

| Loop | Cadence | Level | Entry point |
|---|---|---|---|
| Conductor mission | on demand, hour-budgeted | L3 gated | `oracle mission <toml> [engine] [hours]` |
| Hourly report + overseer | every `report_minutes` (default 60m) inside a mission | L1 report | automatic |
| Regression sweep | after every merge inside a mission | L2 | automatic |
| Retrospective + amendments | mission end, or `oracle retro` | L1 propose-only | automatic |
| Session journals | every agent session (start/compact/end) | L1 | automatic (hooks) |

## Budget and kill switches

- Every mission carries an explicit **hour budget** (`--hours`, default 24);
  dispatch stops 10 minutes before the deadline.
- Per-task budgets: `timeout_minutes` (total) and `stall_minutes` (output
  silence) — the watchdog kills runs that exceed either.
- Attempts are capped (`max_attempts`, default 3); the final attempt escalates
  tier instead of blind-retrying (seed brain A5).
- Infrastructure failures refund attempts and cap at 3 INFRA strikes before the
  task is blocked with an honest note (E19).
- Kill switches: Ctrl+C on the conductor (state survives on disk; relaunch
  resumes from worktrees + memory), `oracle stop` for serving.
- Token/compute cost is inherently capped by local serving: one big slot, one
  fast lane, no metered API. Estimate pattern costs with `oracle loops cost`.

## Safety gates and auto-merge policy

- **No auto-merge to the main line.** Task work happens in isolated worktrees;
  merges land on the mission branch only after (1) deterministic checks exit 0,
  (2) an independent auditor passes it, (3) an opus-tier tiebreak on
  disagreement, (4) an optional adversary pass on risk-bearing tasks. The
  operator merges mission branches into main.
- **Irreversible work requires a countersign**: tasks with
  `requires_approval = true` wait for `APPROVE <task-id>` in
  `memory/APPROVALS.md` (three-party rule, seed brain G6).
- **Network is structurally denied** to workers; only the envoy fetches, in
  operator-opened windows, from an allowlist, into quarantine
  (`connectors/net-allowlist.txt`, `bootstrap/envoy.sh`).
- **Gate-gaming is a FAIL**: auditors treat weakened or deleted tests as
  failure reasons (G4); the regression sweep reopens tasks whose gates break.

## State and memory

- `memory/STATE.md` — generated snapshot (mission, tasks, attempts).
- `memory/LEDGER.md` — append-only event journal (single writer: conductor).
- `memory/FAILURES.md`, `memory/LESSONS.md`, `memory/PROCESS.jsonl` — failure
  memory, distilled lessons, structured telemetry for retrospectives.
- `memory/sessions/*.md` — per-session journals (DOING / DONE / NEXT).
- Layer 0 founding memory: `engines/shared/SEED-BRAIN.md` (principles, errata,
  NEW PRINCIPLES promoted from incidents).

## Constraints

- One mission at a time per machine (the big model slot is exclusive).
- Worktrees isolate every task; the conductor is the only merger (A4/O3).
- Least privilege by persona: researcher/auditor/adversary are read-only +
  test execution; the envoy's bash is allowlisted to fetch tooling only.
- Week-one posture for any NEW loop pattern: report-only (L1) before assisted
  (L2) before unattended (L3) — see `skills/loop-engineering/SKILL.md`.

## Observability

- Hourly `reports/REPORT-*.md` with the overseer's time-use verdict.
- Mission control web UI: `oracle console` (127.0.0.1:8800).
- Orchestration viewer (optional): `oracle agents-ui` (Agent-MCP,
  127.0.0.1:3847) — agents, tasks, and shared context as a live graph.
- `oracle state`, `oracle report`, `oracle ledger [n]` from any shell.
