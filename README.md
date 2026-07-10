# SentiVue Oracle

A **self-contained development ecosystem**: an offline, self-governing, self-improving
agentic workstation for quantitative research, trading-system development, and machine
learning. The appliance runs entirely on a Mac Studio (512 GB unified memory) against
local open-weight models; a Windows node handles authoring, model pre-downloading, and
carries the same private git vault. Every dependency of the development loop — models,
inference, engines, skills, data, memory, and version control — lives inside the
ecosystem. After the one-time bootstrap, no piece of it needs the internet.

**Engines: Claude Code or OpenCode — your choice, same models, same skills.** Cursor is
deliberately not part of this stack. All services bind to `127.0.0.1`, all telemetry is
disabled, and an optional firewall profile blocks everything else.

---

## Architecture

```
                        ┌─────────────────────────────────────┐
                        │        conductor (missions)          │
                        │  24h self-governing loop · worktrees │
                        │  auditors · hourly reports · ledger  │
                        └──────────────────┬──────────────────┘
                                           │ headless runs
        ┌──────────────────────────────────┴──────────────────────────────────┐
        │                        ENGINE (pick per session)                     │
        │   Claude Code (offline-hardened)      OpenCode (fully open source)   │
        │   + ECC harness subset + skills       + same skills + agents         │
        └───────────────┬──────────────────────────────────┬──────────────────┘
                        │ Anthropic /v1/messages            │ OpenAI /v1
                        └─────────────────┬─────────────────┘
                                          ▼
                       llama-swap · http://127.0.0.1:9099
                       ┌──────────────────────────────────┐
                       │ BIG SLOT (one resident at a time) │
                       │  qwen3-coder-480b   (sonnet tier) │
                       │  kimi-k2-thinking   (opus tier)   │
                       │  deepseek-v3.2      (alt reasoner)│
                       ├──────────────────────────────────┤
                       │ FAST LANE (always resident)       │
                       │  qwen3-coder-30b    (haiku tier)  │
                       │ EMBEDDINGS (always resident)      │
                       │  qwen3-embedding-4b               │
                       └──────────────────────────────────┘
                                          ▼
                        llama-server (llama.cpp · Metal)
                        Mac Studio · 512 GB unified memory
```

- **llama-swap** is the single front door. It speaks both the Anthropic Messages API
  (for Claude Code) and the OpenAI API (for OpenCode), and hot-swaps the big model by
  requested model name. The fast lane and embeddings stay resident permanently.
- **Tier mapping**: `opus → kimi-k2-thinking`, `sonnet → qwen3-coder-480b`,
  `haiku → qwen3-coder-30b`. Background/subagent traffic rides the fast lane so the
  big slot never thrashes.
- **ECC** (pinned, curated subset) provides harness discipline: continuous learning,
  worktree lifecycle, orchestration commands, language rule packs. Cloud-dependent
  operators are stripped by the installer.
- **conductor** runs long missions: plans tasks, dispatches engine runs into isolated
  git worktrees, audits every result with a second agent pass, self-heals stalls and
  crashes, files hourly reports, and keeps a plain-text memory ledger.

## Repository layout

```
install         guided installer (checkpointed phases — re-run to resume)
bin/oracle      single CLI entry point, symlinked onto PATH by the installer
bootstrap/      setup phases, model downloads, doctor, verify, harden, uninstall, packaging
serving/        model manifest + profile, rendered llama-swap config, launchd service
engines/        claude-code/ and opencode/ configs, subagents, conventions
harness/ecc/    pinned ECC version + curated-subset installer
skills/         10 domain skill packs (engine-agnostic SKILL.md)
connectors/     MCP servers (DuckDB, Postgres), self-hosted Supabase compose
conductor/      mission daemon, mission specs
env/            uv-managed Python quant stack
memory/         plain-text ledger + state (runtime, gitignored)
```

## Getting it onto the Mac (privacy-friendly)

On the authoring machine: `make dist` produces a clean tarball (no models, no
secrets, no caches). Move it by USB/AirDrop — or push to a private git remote if
you have one. No cloud service is required.

### Pre-downloading the models on Windows (optional, saves a night)

The ~700 GB model download can run on a Windows machine ahead of time — ideally
straight onto an exFAT external drive:

```powershell
powershell -ExecutionPolicy Bypass -File bootstrap\download-models.ps1 -Dest E:\oracle-models
```

Same manifest/profile as the Mac scripts, resumable, auto-retries. Then on the
Mac either copy the folder to `~/sentivue-oracle/models` or symlink it
(`ln -s /Volumes/<drive>/oracle-models ~/sentivue-oracle/models`) and the
installer's download phase will see everything already present and skip it.

## Quickstart (on the Mac Studio)

```bash
tar -xzf sentivue-oracle-*.tar.gz && cd sentivue-oracle
bash install
```

The installer walks through: preflight (hardware/disk checks) → bootstrap (brew,
pinned engines, python env, skills, ECC) → **model profile choice**
(`full` ~700 GB · `coder` ~315 GB · `minimal` ~40 GB smoke test) → resumable
downloads → serve → offline verification. Every phase is checkpointed; re-running
`bash install` resumes after a failure or an interrupted download. Reduced
profiles automatically remap the opus/sonnet tiers onto models that exist.

Afterwards, everything is one command:

```bash
oracle claude        # interactive session, Claude Code engine
oracle opencode      # interactive session, OpenCode engine
oracle mission conductor/missions/example.toml claude 24
oracle status        # service + models + ledger tail
oracle doctor        # full diagnostic with suggested fixes
oracle harden        # software air gap (pf egress block); undo: oracle harden off
```

(`make …` targets remain for everything if you prefer; `oracle uninstall`
cleanly removes services, symlinks, and — with `--purge` — the models.)

## Engine choice

| | Claude Code | OpenCode |
|---|---|---|
| Source | proprietary CLI, free to run against local endpoints | 100% open source |
| Wire protocol | Anthropic `/v1/messages` → llama-swap | OpenAI `/v1` → llama-swap |
| Offline posture | hardened via env (`DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, autoupdater off) | no account, no telemetry; models.dev cache warmed at bootstrap |
| Harness quality | strongest agentic harness; ECC-native | very close; reads the same `AGENTS.md`, skills, MCP config |
| Conductor support | `claude -p` headless | `opencode run` headless |

Both engines read the same skills, subagents, conventions (`AGENTS.md`), and MCP
connectors. Switching engines is a per-session decision, not a migration.

## Local git vault (the ecosystem's own "origin" — on every node)

A self-contained ecosystem owns its version control: the vault is a directory of
history-protected bare git repositories (`~/oracle-git-vault` /
`%USERPROFILE%\oracle-git-vault`, configurable via `ORACLE_VAULT`) — a private
local origin with zero dependencies beyond git. Vault repos refuse deletes and
non-fast-forward pushes (append-only history). It ships for **both nodes** with
identical commands:

```bash
# Mac appliance (bash)                    # Windows node (PowerShell)
oracle vault init                         bin\oracle.ps1 vault init
oracle vault sync [repo]                  bin\oracle.ps1 vault sync [repo]
oracle vault new my-strategy              bin\oracle.ps1 vault new my-strategy
oracle vault clone my-strategy            bin\oracle.ps1 vault clone my-strategy
oracle vault list                         bin\oracle.ps1 vault list
oracle vault backup /Volumes/usb          bin\oracle.ps1 vault backup E:\
```

- **Mac:** the installer creates the vault and registers the `vault` remote; the
  conductor auto-pushes every merged mission branch and everything at mission end —
  autonomous work is continuously backed up off the working copy. `oracle doctor`
  reports vault currency.
- **Windows:** `finish-windows.ps1` mirrors every commit to the vault alongside the
  GitHub push, and the project rule keeps them in lockstep — so even the authoring
  node's history survives without any cloud dependency.
- `vault sync <path>` auto-creates a bare repo for any new project on first push;
  `vault backup` tarballs the whole vault for external/USB rotation. Moving work
  between nodes offline is an ordinary `git fetch` from a vault backup on a drive.

## Privacy posture

- No Cursor, no hosted APIs, no accounts. Models, inference, data, and memory never
  leave the machine.
- Every service binds `127.0.0.1` only (llama-swap, llama-server, Postgres, PostgREST, Studio).
- Telemetry, error reporting, and auto-update are disabled for both engines.
- `make harden` installs a pf anchor that blocks ALL outbound traffic machine-wide
  except loopback — a software air gap (optional, reversible with `harden-offline.sh off`).
- The only network phase is bootstrap: Homebrew, npm pins, model downloads, docker
  image pulls, uv cache warm. `make verify` proves the stack works with networking off.

## Model ensemble (defaults, editable in `serving/models.manifest`)

| Model | Role | Quant | ~Size | Notes |
|---|---|---|---|---|
| Qwen3-Coder-480B-A35B | primary coder (sonnet) | UD-Q4_K_XL | 276 GB | daily driver, 131k ctx |
| Kimi-K2-Thinking | deep reasoner (opus) | UD-Q2_K_XL | 381 GB | planning/architecture, 98k ctx |
| DeepSeek-V3.2 | alt reasoner | UD-Q3_K_XL | 320 GB | swap-in alternative |
| Qwen3-Coder-30B-A3B | fast lane (haiku) | Q8_0 | 33 GB | subagents, background, always on |
| Qwen3-Embedding-4B | embeddings | Q8_0 | 5 GB | RAG/docs, always on |

Big-slot models swap on demand (~1–2 min load from SSD); fast lane + embeddings are
permanently resident. Default GPU wired limit is raised to 448 GB by the installer
(`iogpu.wired_limit_mb`), leaving headroom for the OS. `serving/models.profile`
(written by the installer) controls which rows are active on this machine.

## The self-governing loop

`conductor/conductor.py` implements the mission contract:

- **Automations** — mission TOML defines goal, tasks, dependencies, acceptance criteria;
  the loop continuously answers "what needs to be done? what changed?"
- **Planning** — `auto_plan = true` missions need only a goal: an opus-tier planner
  investigates the repo and emits the task DAG (acceptance criteria + mechanical checks
  included); if the DAG later stalls on failures, one bounded replan routes around them
  (see `conductor/missions/autonomous.toml`).
- **Worktrees** — every task executes in its own `git worktree`; agents cannot collide;
  merges happen only after the full verification stack passes.
- **Verification stack** — layered, cheapest-first: (1) deterministic `checks` — shell
  commands the conductor runs itself, every one must exit 0; (2) sonnet-tier auditor
  (verifier as strong as the generator); (3) opus-tier tiebreak when checks and auditor
  disagree; (4) optional adversary pass on risk-bearing tasks.
- **Escalation ladder** — a task's final attempt automatically runs on the opus tier:
  maximum intelligence exactly where the budget shows it's needed.
- **Supervision** — output-stall detection (default 12 min of silence) kills runaway
  runs early instead of burning the full time box; total timeouts back it up; failed
  attempts hand the next attempt a full `FEEDBACK.md` plus a forced root-cause
  diagnosis so retries change strategy instead of repeating.
- **Throughput** — `workers = 2` runs haiku-tier tasks on the always-resident fast lane
  in parallel with big-slot work; the machine never idles while one model thinks.
- **Skills** — engines load the domain packs; the ECC `continuous-learning` skill plus
  the conductor's ledger feed new `SKILL.md` candidates back into `skills/`.
- **Connectors** — MCP: DuckDB data lake, Postgres/Supabase (self-hosted), filesystem, git.
- **Subagents** — `researcher`, `developer`, `auditor`, `adversary` for both engines.
- **Memory** — append-only `memory/LEDGER.md` + `memory/STATE.md` snapshot +
  `memory/FAILURES.md` (approaches that failed and why — read before every attempt) +
  `memory/LESSONS.md` (distilled at mission end, read at every start — mission N+1
  starts smarter than mission N), plain text, the single source of truth across runs.
- **Regression sweeps** — after every merge, ALL previously-done tasks' checks re-run
  against the advanced mission branch; a later merge that breaks an earlier gate
  reopens that task. Done stays done.
- **Operator countersign** — tasks marked `requires_approval` (the planner assigns it
  to irreversible work, which is always split dry-run → execute) hold until the
  operator writes `APPROVE <id>` into `memory/APPROVALS.md`; reports surface the queue.
- **Self-healing** — llama-swap health checks with automatic service restart, bounded
  retries, idle-time work queue, hourly `REPORT-*.md`, and a `FINAL-REPORT.md`.
- **Long-horizon protocol** — `engines/shared/AUTONOMY.md`, loaded globally by both
  engines and enforced by the conductor's task prompts: session-start recovery ritual,
  plan-first (`TASKPLAN.md` with per-step checks and a NOT-DOING list), ratchet commits
  (`bin/checkpoint`), evidence-or-it-didn't-happen, a two-strike stuck protocol that
  forces strategy changes, context-rot detection with clean checkpointing, and
  failure-memory discipline.
- **Self-improvement** — the loop documents its own behavior (`memory/PROCESS.jsonl`
  telemetry, transcripts, TASKPLAN decision logs, ledger `friction:` lines), and every
  mission ends with a retrospective (`oracle retro` runs one on demand) that scores
  prior amendments against their registered success criteria, diagnoses process
  bottlenecks, and proposes at most 3 protocol amendments — each a pre-registered
  experiment applied only through an operator-countersigned amendment mission and
  reverted if its criterion comes back NOT-MET (AUTONOMY §13, Amendment Log).

## Honest expectations

A 512 GB Mac Studio running Kimi K2 / Qwen3-Coder-480B is roughly frontier-minus-one:
excellent on well-specified engineering tasks, weaker on very long autonomous horizons.
The harness exists precisely to close that gap: long-horizon failures are mostly
process failures (goal drift, context rot, repeated dead ends, false completion), so
the protocol + auditors + failure memory attack them mechanically rather than hoping
the model stays sharp for 24 hours. Prompt prefill is the bottleneck on Apple Silicon —
keep contexts tight and let the fast lane absorb background traffic.
