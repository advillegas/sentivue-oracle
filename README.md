# SentiVue Oracle

A **self-contained development ecosystem**: an offline, self-governing, self-improving
agentic workstation for quantitative research, trading-system development, and machine
learning. It runs the full platform on **any machine**: the installers detect your
hardware (RAM/VRAM) and suggest the right model profile — from the 512 GB Mac Studio
flagship (`full`, ~700 GB ensemble) down to a 16 GB laptop (`micro`, ~10 GB) — and
remap the model tiers so everything works at every size. Windows and macOS are both
first-class: local model serving (llama-swap + llama.cpp Vulkan/Metal), both engines,
the desk app, the vault, and the conductor run on either. Every dependency of the
development loop — models, inference, engines, skills, data, memory, and version
control — lives inside the ecosystem. After the one-time bootstrap, no piece of it
needs the internet.

**Engines: Claude Code, OpenCode, or Kilo Code — your choice, same models, same
skills.** Cursor is deliberately not part of this stack. All services bind to
`127.0.0.1`, all telemetry is disabled, and an optional firewall profile blocks
everything else.

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
        │   Claude Code (offline-hardened) · OpenCode (open source)            │
        │   Kilo Code (OpenCode fork, shared with the IDE panel)               │
        │   + ECC harness subset + the same skills + agents everywhere         │
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

**Double-click installers** live on the repo's [Releases page]
(https://github.com/advillegas/sentivue-oracle/releases) — no commands needed:

- **Mac:** `SentiVue-Oracle-Installer-<ver>.command` — double-click; a Terminal
  wizard prompts through install location → tools → model profile (full/coder/
  minimal) → downloads → verification. Downloaded `.command` files need one
  right-click → Open (Gatekeeper) the first time.
- **Windows:** `SentiVue-Oracle-Setup-<ver>.cmd` — double-click; a console wizard
  prompts through location → git vault setup → optional model pre-download.

Both are self-extracting (the full repo rides inside, built from `git archive` so
they're always clean) and every step is resumable. Plain `.tar.gz`/`.zip` archives
sit alongside for scripted installs; the repo is private, so downloads authenticate
via `gh release download` or a signed-in browser. Publishing a new version:
`bootstrap/release.ps1 -Version vX.Y.Z`.

Fully offline alternative: `make dist` produces the tarball locally for USB/AirDrop,
or plain `git clone` from the vault.

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
oracle kilo          # interactive session, Kilo Code engine (TUI)
oracle mission conductor/missions/example.toml claude 24   # engine: claude|opencode|kilo
oracle status        # service + models + ledger tail
oracle doctor        # full diagnostic with suggested fixes
oracle harden        # software air gap (pf egress block); undo: oracle harden off
```

(`make …` targets remain for everything if you prefer; `oracle uninstall`
cleanly removes services, symlinks, and — with `--purge` — the models.)

## Engine choice

| | Claude Code | OpenCode | Kilo Code |
|---|---|---|---|
| Source | proprietary CLI, free to run against local endpoints | 100% open source | open source (OpenCode fork) |
| Wire protocol | Anthropic `/v1/messages` → llama-swap | OpenAI `/v1` → llama-swap | OpenAI `/v1` → llama-swap |
| Offline posture | hardened via env (`DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, autoupdater off) | no account, no telemetry; models.dev cache warmed at bootstrap | no account needed for local providers; telemetry off, sharing disabled in generated config |
| Harness quality | strongest agentic harness; ECC-native | very close; reads the same `AGENTS.md`, skills, MCP config | OpenCode harness + Kilo modes; same config surface as the IDE side panel |
| Conductor support | `claude -p` headless | `opencode run` headless | `kilo run --auto` headless |

All three engines read the same skills, subagents, conventions (`AGENTS.md`), and MCP
connectors; Kilo additionally shares `~/.config/kilo/kilo.jsonc` with the IDE panel,
so CLI, missions, and side panel always agree on models and permissions. Switching
engines is a per-session decision, not a migration.

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

## Controlled internet: the envoy (a dedicated security-layer agent)

Workers are permanently offline — their engines deny every network tool, firewall or
not. When the ecosystem genuinely needs something from outside (a library, an MCP
server, documentation), that goes through the **envoy**: a dedicated internet agent
that runs only in operator-opened windows (`oracle envoy`) and is fetch-only by
construction:

- Its sole network tool is `bin/envoy-fetch`: HTTPS **GET** to domains in
  `connectors/net-allowlist.txt` (registries + docs only — no search engines, since
  URLs are an exfiltration channel), no query strings, capped size, version pins
  required.
- Everything it downloads is **quarantined** in `incoming/` with sha256 + source in
  `incoming/PROVENANCE.md`. The envoy never installs or executes what it fetched;
  workers install from the local quarantine and never fetch. One-way valve.
- **Discovery without leakage** (`bin/envoy-discover`): sanitized queries to
  structured public-knowledge APIs — Stack Overflow, GitHub issues/repos, PyPI,
  npm, crates.io, arXiv, Hugging Face, Wikipedia. The sanitizer enforces length,
  charset, and token caps, rejects encoded-data-looking strings, and refuses any
  query containing identifiers from `connectors/discovery-blocklist.txt` (project
  names and internals that must never leave). Every query is audit-logged. This
  covers the long-tail-debugging and "find the right tool/paper" gaps while
  keeping general search engines off.
- Workers queue needs in `memory/NET-REQUESTS.md` (`NEED pip:pkg==x.y.z` for
  artifacts, `FIND <generalized question>` for research); `oracle envoy --queue`
  processes the queue headlessly; the air-gap is dropped for the window and
  **restored automatically on exit** (trap), even if the session crashes.
- Doctrine in `engines/shared/ENVOY.md`; engine-level permission sets pin the
  envoy to exactly this behavior on both Claude Code and OpenCode.
- **Deferred (by decision, noted for later):** a fully local research library —
  Kiwix archives of Stack Overflow (~75 GB) and Wikipedia (~100 GB) plus bulk
  arXiv packs, indexed into the existing pgvector RAG — which would move most
  discovery entirely offline. Fetching the archives is a standing envoy job
  whenever storage planning allows.

## The frontend: oracle-desk (native Rust, no web)

`oracle desk` — the platform's face. A single-binary Rust desktop app (`desk/`,
egui/eframe: native rendering, **no webview, no Electron, no browser**) that talks
to the ecosystem through its real interfaces:

- **Chat** — drives Claude Code headlessly over its structured `stream-json`
  protocol (streamed replies, visible tool calls, multi-turn via session resume)
  and OpenCode via `run --continue`; engine switcher in the toolbar. Every token
  is generated by the local models.
- **Missions** — reads the plain-text state directly from disk (the files are the
  API): live mission state, one-click APPROVE countersigns, envoy queue, ledger,
  clickable reports.
- **Models** — llama-swap health + resident/running models over localhost.
- **Vault** — repository inventory and this repo's sync currency.

First run builds it (`brew install rust` + `cargo build --release`, once); after
that it's a native binary. The desktop-shortcut menu has it as option `0`.

## Supporting UIs (all localhost)

- **`oracle ide`** — the Cursor-like IDE: **VSCodium** (telemetry-free VS Code — the
  same platform Cursor is built on) with **Continue** (codebase chat `Cmd+L`, inline
  edits `Cmd+I`, tab autocomplete on the fast lane, embeddings-backed codebase
  indexing) and **Kilo Code** (the agentic composer panel: plan/act, file edits,
  terminal — the maintained successor to the discontinued Roo Code, configured
  local-provider-only with telemetry and session sharing disabled) — all pointed
  at llama-swap, so every keystroke of AI runs on local models. `oracle ide
  install` sets it up (extensions from open-vsx, configs generated, updates and
  telemetry off); `oracle ide` opens the repo.
- **Parallel agent tabs** — `Cmd+Shift+A` (`Ctrl+Shift+A` on Windows) opens a full
  engine session (Claude Code) as an editor tab; open as many as you want, side by
  side, like Cursor's agent tabs. `Cmd+Shift+Alt+A` opens the agent in its **own git
  worktree + branch** so parallel agents never collide — merge the branch when you
  like the result. `Cmd+Alt+O` opens an OpenCode tab; the Agents sidebar also offers
  **Kilo Code** tabs (the Kilo CLI — same models and `kilo.jsonc` as the side panel).
  Also available from the terminal `+` dropdown ("Oracle Agent" profiles).
- **Model auto-detection** — every IDE launch runs `sync-models`, which asks the
  serving layer (or scans `models/`) for what's actually installed, then rewires
  Continue, Kilo Code (generated `~/.config/kilo/kilo.jsonc`), the engine tier maps
  (`serving/tiers.env`), and the opus/sonnet/haiku aliases to models that exist on
  this machine. Download a new model and it shows up everywhere on the next launch.
- **`oracle agents-ui`** (optional) — the **orchestration viewer**: a vendored,
  pinned [Agent-MCP](https://github.com/rinadelph/Agent-MCP) deployment pointed
  entirely at llama-swap (its embeddings ride the local `text-embedding-3-large`
  alias) and bound to loopback. Watch how agents are orchestrated — agents,
  tasks, and shared context as a live knowledge graph at `http://127.0.0.1:3847`,
  with the coordination server (MCP endpoint) on `:8100`. Engines can OPT IN to
  its create_agent/assign_task/RAG tools: Claude Code via
  `--mcp-config connectors/mcp.agent-mcp.json`, OpenCode by flipping the
  `agent-mcp` entry in `opencode.json` to `"enabled": true`. Also launchable
  from the Agents sidebar ("Orchestration Viewer").
- **`oracle loops`** — the vendored, pinned
  [loop-engineering](https://github.com/cobusgreyling/loop-engineering) toolkit:
  `audit` (Loop Readiness Score for this repo), `init` (scaffold new loop
  patterns), `cost` (token estimates), `sync` (STATE/LOOP drift detection). The
  7 production loop patterns are distilled into `skills/loop-engineering` for
  both engines, and `LOOP.md` documents this platform's own loops, budgets, and
  kill switches in the same convention.
- **`oracle notes`** (optional) — **Obsidian over the repo itself**: the entire
  knowledge surface (seed brain, doctrine, ledger, lessons, failures, session
  journals, mission reports, decision records — ~300 markdown files) is already
  plain text, so the repo opens directly as a vault. Checked-in `.obsidian/app.json`
  excludes heavy runtime dirs from indexing; no sync, no account, not in the agent
  path — purely the operator's search/navigate/edit lens (decision 0002).
- **`oracle console`** — mission control at `http://127.0.0.1:8800`: live mission
  state, one-click operator approvals, the network-request queue, ledger tail, and
  reports (stdlib Python, zero dependencies).
- **`oracle vault ui install`** — Gitea at `http://127.0.0.1:3300`: the GitHub-like
  face of the vault (browse, diffs, blame, search). Vault repos are added as
  local-path **mirrors**, so the vault stays the source of truth and Gitea re-syncs
  hourly. Sqlite, registration disabled, offline mode, `launchd`-supervised.
- Terminal alternatives remain (`oracle claude` / `oracle opencode` / `oracle kilo`);
  llama-swap's built-in UI at `:9099` shows model activity.

## Privacy posture

- No Cursor, no hosted APIs, no accounts. Models, inference, data, and memory never
  leave the machine.
- Every service binds `127.0.0.1` only (llama-swap, llama-server, Postgres, PostgREST,
  Studio, Gitea, the console).
- Telemetry, error reporting, and auto-update are disabled for all engines
  (Kilo additionally runs with sharing disabled and local providers only).
- `make harden` installs a pf anchor that blocks ALL outbound traffic machine-wide
  except loopback — a software air gap (optional, reversible with `harden-offline.sh off`).
  `oracle envoy` opens it briefly and always restores it.
- Network touches the machine in exactly two ways after bootstrap: envoy windows
  (fetch-only, allowlisted, quarantined, provenance-tracked) — and nothing else.
- The only broad network phase is bootstrap: Homebrew, npm pins, model downloads,
  docker image pulls, uv cache warm. `make verify` proves the stack works offline.

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
- **Subagents** — `researcher`, `developer`, `auditor`, `adversary`, `librarian`
  (memory curator), `envoy` (controlled network) for both engines; plus conductor-run
  roles: `planner`, `overseer` (hourly time-use audit), `historian`, `meta-analyst`.
- **Seed brain** — `engines/shared/SEED-BRAIN.md`: ~90 stable-ID principles
  (orchestration, agent assignment, loop engineering, context integrity, error
  handling, verification, governance, memory) distilled by a frontier meta-analysis
  of the operator's full project history. Personas load role-scoped digests, the
  protocol enforces its top findings, agents cite principle IDs in ledgers and
  incident records, and corrections flow through its append-only errata — the
  platform starts post-pain instead of re-deriving each failure.
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
