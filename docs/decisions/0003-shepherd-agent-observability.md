# Decision 0003 — Shepherd (agent tracing): don't adopt; we already own the data

Date: 2026-07-11 · Status: DECIDED (operator may overturn) · Seed brain: G10
(dependency pulse), M7 (validated telemetry), V10 (layered verification)

## What "Shepherd" is (two distinct projects, verified 2026-07-11)

1. **Shepherd observability** (neuralis-in): CLI-first agent tracing on the
   `aiobs` SDK (Python/TS, MIT) + an MCP server so IDE agents can search/diff
   agent sessions. Concept: every LLM call, tool call, and decision becomes a
   JSON trace; sessions are searchable and diffable.
   **G10 pulse: FAILS.** aiobs has 8 stars, shepherd-mcp has 0, last pushes
   2026-01-03 (six months dormant), no releases of note. Adopting it means
   instrumenting our conductor and engines with a dead micro-SDK.
2. **SHEPHERD meta-agent substrate** (arXiv 2605.10913, 2026): a research
   runtime where agent execution is a reversible, Git-like trace — meta-agents
   fork, rewind, and repair worker runs (supervisor meta-agent lifted pair-coding
   pass rates 28.8%→54.7% on CooperBench). Impressive, open-source, and
   architecturally aligned with our conductor — but it is a Python substrate
   that agents must be WRITTEN INSIDE. Our engines are external processes
   (Claude Code/OpenCode); rehosting them is a rewrite, not an integration.

## Decision

**Adopt neither as a dependency.** The observability tool fails the pulse
check; the substrate is a research runtime that would replace our execution
model rather than observe it.

**The capability, however, we already have in raw form and should finish
ourselves:** every conductor engine run is captured as complete stream-json in
`logs/<mission>-<task>-<phase>.log` — the full trace of every LLM message,
tool call, tool result, timing, and token count. That IS the Shepherd trace;
it needs a query surface, not a new collector.

## Queued follow-up (mission-sized, local, zero new deps)

`bin/trace` — a small CLI over our existing logs (candidate mission task):
- `trace list [mission]` — sessions with duration, turns, tokens, outcome
- `trace show <log>` — the tool-call tree with timings (what Shepherd calls
  the execution timeline)
- `trace diff <a> <b>` — attempt-over-attempt comparison (what changed between
  dev1 and dev2: tools used, files touched, failure point)
- `trace grep <pattern>` — search across all sessions
This closes the observed debugging gap (we hand-parsed these logs during every
incident this week) and keeps the data on disk, in our format, greppable by
agents (C4) and viewable by the operator.

## What we take from the substrate paper (concepts, not code)

- Their supervisor's "intercept before execution" = our approval gates (G6) —
  validated by their CooperBench numbers.
- Counterfactual repair (replay from the point of changed behavior) = a
  sharper version of our FEEDBACK.md retry; if the loop ever needs it, the
  worktree + attempt structure is the coarse-grained equivalent to build on.
- Revisit if SHEPHERD gains real adoption as an execution substrate for
  EXTERNAL engines (not just in-substrate @task agents).
