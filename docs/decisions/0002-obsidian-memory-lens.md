# Decision 0002 — Obsidian as the operator's memory lens (optional)

Date: 2026-07-11 · Status: ADOPTED (optional component) · Seed brain: M1 (plain
text beats clever storage), C4 (agents keep grep; humans get a lens)

## Context

The platform's entire knowledge surface is already plain markdown on disk —
~300 files: the seed brain and doctrine (`engines/shared/`), runtime memory
(`memory/`: ledger, state, lessons, failures, session journals), mission
reports (`reports/`), skills, and decision records. M1 chose plain text so
"every agent and every human can read it under any failure mode." Agents read
it with grep. The human deserves better than `type memory\LEDGER.md`.

## Decision

Adopt **Obsidian** as the optional, operator-side viewer over the repo itself:

- **The repo is the vault.** No export, no sync, no second copy — Obsidian
  opens the working tree directly; `.obsidian/app.json` is checked in with
  heavy/runtime dirs excluded from indexing (models, toolchains, worktrees,
  vendor checkouts). The volatile rest of `.obsidian/` is gitignored.
- **Local-first fits the constitution:** Obsidian reads local files, needs no
  account, and its optional sync/publish services are simply not used. The
  machine firewall applies to it like everything else. It is closed-source,
  which is acceptable for a HUMAN viewer (it is not in the agent path, has no
  model access, and touches nothing the agents depend on).
- **Zero platform coupling.** Nothing reads `.obsidian/`; deleting it changes
  nothing. Agents continue to use grep/ripgrep (C4: no always-on injection).
- `oracle notes` opens the vault (installs Obsidian on first use via
  brew/winget where available).

## What it buys the operator

- Full-text search + quick-switch across ledger, lessons, failures, seed brain,
  reports, decisions, session journals.
- Folding/outline for the long doctrine files; graph and backlinks that grow
  if `[[wiki-link]]` conventions are adopted later (see Future).
- A safe editing surface for countersigning approvals, curating LESSONS.md,
  and reviewing retrospectives.

## Future (not now)

- Linking conventions: `[[SEED-BRAIN]]` / principle-ID anchors in ledger and
  incident entries would light up the graph view; a candidate mission task —
  only if the operator actually uses the graph.
- Obsidian Bases/Dataview-style dashboards over reports: revisit on demand.

## Rejected alternatives

- **Gitea wiki / console web UI** — already exist for their niches (vault
  browsing, mission control); neither is a knowledge-navigation tool.
- **Building a custom viewer** — violates "don't rebuild what a local tool
  does better"; the memory system's value is that it needs no custom tooling.
