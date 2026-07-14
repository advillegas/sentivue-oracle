# SentiVue Oracle

A **self-contained development ecosystem** for offline quantitative research,
trading-system development, and machine learning. Its serving layer selects from
declared hardware profiles, validates policy-bound model snapshots, and generates a
loopback-only runtime plan under `state/generated/`.

Runtime certification is provisional until `oracle verify` passes the
production-shaped compatibility, context-boundary, listener, and model-identity
probes on the target machine. Windows and macOS expose matching
`service`/`capabilities`/`verify`/`doctor` surfaces, but inferred CUDA, Vulkan, Metal,
or CPU capability is never presented as proof that llama.cpp loaded or offloaded a
model. Unsupported profiles, missing model authorities, and unsafe contexts fail
closed.

**Engines: Claude Code, OpenCode, or Kilo Code.** Cursor is deliberately not part of
this stack. Core serving is constrained to loopback; optional components and firewall
coverage remain platform-scoped and must be inspected on the deployed host.

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
                  Oracle admission gateway · http://127.0.0.1:9099
                    llama-swap internal · 127.0.0.1:9098
                       ┌──────────────────────────────────┐
                       │ BIG SLOT (exclusive admission)    │
                       │  qwen3-coder-480b   (sonnet tier) │
                       │  kimi-k2-thinking   (opus tier)   │
                       │  deepseek-v3.2      (alt reasoner)│
                       ├──────────────────────────────────┤
                       │ FAST LANE (configured resident)   │
                       │  qwen3-coder-30b    (haiku tier)  │
                       │ EMBEDDINGS (configured resident)  │
                       │  qwen3-embedding-4b               │
                       └──────────────────────────────────┘
                                          ▼
                    llama-server (explicit selected backend)
                     target hardware validated at runtime
```

- The **Oracle admission gateway** rejects requests that exceed the generated
  per-slot context or safe concurrency before forwarding to llama-swap. The verifier
  checks OpenAI, Anthropic, tools, JSON, embeddings, and cold/warm behavior.
- **Tier mapping** is profile-specific. The full profile maps to the large models
  shown above; reduced profiles intentionally collapse tiers only to models present
  in the same policy-bound snapshot.
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
serving/        model declarations + thin Windows Scheduled Task/macOS launchd twins
verification/   shared resource, profile, admission, render, and probe implementation
engines/        claude-code/ and opencode/ configs, subagents, conventions
harness/ecc/    pinned ECC version + curated-subset installer
skills/         10 domain skill packs (engine-agnostic SKILL.md)
connectors/     MCP servers (LeanCTX, DuckDB, Postgres), self-hosted Supabase compose
conductor/      mission daemon, mission specs
env/            uv-managed Python quant stack
memory/         plain-text ledger + state (runtime, gitignored)
```

## Install

The [Releases page](https://github.com/advillegas/sentivue-oracle/releases)
offers exactly two downloads:

- **Windows Installer** — `SentiVue-Oracle-Setup-<ver>.cmd`. Double-click it.
- **Mac Installer** — `SentiVue-Oracle-Installer-<ver>.pkg`. Double-click it.

Because the Mac build is not yet Apple-notarized, macOS blocks the first open.
One-time unblock (about 30 seconds): double-click the `.pkg` and click **Done**
on the "could not verify" dialog, then open **System Settings -> Privacy &
Security**, scroll down to the Security section, click **Open Anyway** beside
the blocked-installer message, and confirm with your password or Touch ID. On
macOS Sonoma or older, right-click the `.pkg` and choose Open instead. Enrolling
the release in Apple notarization removes this step entirely.

That is the whole install — no unzipping and no commands. The Mac package
publishes the verified source, then opens a Terminal window that finishes the
rest automatically. The installer picks a model profile for your hardware,
downloads everything it needs (checksum-bound dependencies, every model shard,
engines, LeanCTX, and the local IDE), configures local serving, and finishes
on its own. If it gets interrupted, run the same installer again (or
double-click `Resume Install.command` inside the installed folder) and it
resumes where it left off. Existing unowned, different-version, or locally
modified trees are never overwritten.

All other build products (`RELEASE-SHA256SUMS`, `RELEASE-PROVENANCE.json`,
source archives, the `.command.zip` launcher) stay in the tag's workflow
artifacts for verification and scripted installs; they are deliberately kept
off the download page.

Maintainers build both formats locally with
`bootstrap\build-one-click-installers.ps1 -Version vX.Y.Z` on Windows or
`bootstrap/build-one-click-installers.sh --version vX.Y.Z` on macOS; a
pre-exported dependency cache can be baked in with `-DependencyCache <path>`
(`--dependency-cache <path>` on macOS) for fully offline installs.
`bootstrap/release.ps1 -Version vX.Y.Z` is preflight-only; the explicit
`bootstrap/release.ps1 -Version vX.Y.Z -Publish` pushes only an immutable tag.
That tag builds and exercises both formats on native GitHub runners, requires
byte-identical base bundles, then publishes only the two labeled installers.

Acquisition records are
untrusted evidence until their source identity, resolved revision, and digest are
independently verified. Every dependency kind uses the same promotion
boundary: an independent verifier supplies an authority JSON containing the artifact
ID, kind, requested/resolved identities, authoritative HTTPS (or digest-qualified
OCI) URL, and expected SHA-256. Run
`bootstrap/promote-dependency.sh ID AUTHORITY.json` (or
`bootstrap/promote-dependency.ps1 -ArtifactId ID -AuthorityFile AUTHORITY.json`) to
validate it against the named `VERSIONS.lock`/policy keys and update the generated,
tracked `verification/dependency-authorities.json`. The promotion command accepts no
artifact bytes and never computes the expected artifact hash, so newly observed bytes
cannot self-promote. Reproducible validation continues to fail closed until this
separate promotion is committed.

```json
{
  "schema_version": 1,
  "authorities": {
    "uv-darwin-arm64": {
      "kind": "toolchain",
      "requested_version": "0.11.26",
      "resolved_version": "0.11.26",
      "source_url": "https://independently-verified.example/uv.tar.gz",
      "sha256": "<64 lowercase hexadecimal characters>"
    }
  }
}
```

After promotion, local archives (including optional OCI image archives) are imported
with `bootstrap/import-dependency.sh` or `bootstrap/import-dependency.ps1`; import
requires exact agreement with the promoted source identity and digest before granting
policy-bound status. Optional, platform-scoped components are reported separately by
the doctors and do not block unrelated release preflight. A plain
`git clone` from the vault remains available for source-only transfer.

### Pre-downloading the models on Windows (optional, saves a night)

The ~700 GB model download can run on a Windows machine ahead of time — ideally
straight onto an exFAT external drive:

```powershell
powershell -ExecutionPolicy Bypass -File bootstrap\download-models.ps1 -Dest E:\oracle-models
```

Same manifest/profile as the Mac scripts, resumable, auto-retries. A download records
untrusted acquisition evidence only. Independently verify the upstream commit and
every expected shard digest, promote those identities into the tracked
`serving/model-authorities.json`, copy only the selected shards under `models/`, and
run `bootstrap/import-model.ps1` (or `.sh`). Arbitrary local GGUFs and `dynamic`
revisions are never admitted to generated configs or serving.

## Quickstart (on the Mac Studio)

```bash
tar -xzf sentivue-oracle-*.tar.gz && cd sentivue-oracle
bash install
```

The installer walks through: preflight (hardware/disk checks) → policy-bound offline
bootstrap → **model profile choice** (`full`, `coder`, `mid`, `lite`, or `micro`) →
promoted model-snapshot validation → serve → offline
verification. Every phase is checkpointed; re-running `bash install` resumes after a
failure. Reduced profiles remap the opus/sonnet tiers only onto authority-validated
models.

Afterwards, everything is one command:

```bash
oracle claude        # interactive session, Claude Code engine
oracle opencode      # interactive session, OpenCode engine
oracle kilo          # interactive session, Kilo Code engine (TUI)
oracle mission conductor/missions/example.toml claude 24   # engine: claude|opencode|kilo
oracle service status
oracle capabilities  # inferred capability versus loaded/offloaded evidence
oracle verify        # production-shaped read-only runtime probes
oracle doctor        # full diagnostic with suggested fixes
oracle ctx status    # repo-local LeanCTX context runtime health
oracle harden        # software air gap (pf egress block); undo: oracle harden off
```

(`make …` targets remain for everything if you prefer; `oracle uninstall`
previews ownership-scoped removal. Applying it is explicit, and runtime-root
purge requires a second confirmation flag.)

## Engine choice

| | Claude Code | OpenCode | Kilo Code |
|---|---|---|---|
| Source | proprietary CLI, free to run against local endpoints | 100% open source | open source (OpenCode fork) |
| Wire protocol | Anthropic `/v1/messages` → llama-swap | OpenAI `/v1` → llama-swap | OpenAI `/v1` → llama-swap |
| Offline posture | hardened via env (`DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, autoupdater off) | no account, no telemetry; models.dev cache warmed at bootstrap | no account needed for local providers; telemetry off, sharing disabled in generated config |
| Harness quality | strongest agentic harness; ECC-native | very close; reads the same `AGENTS.md`, skills, MCP config | OpenCode harness + Kilo modes; same config surface as the IDE side panel |
| Conductor support | `claude -p` headless | `opencode run` headless | platform-scoped; not certified by the shared serving verifier |

All three engines read the same skills, subagents, conventions (`AGENTS.md`), and MCP
connectors. Kilo's CLI and IDE panel explicitly select the same generated
`state/generated/kilo/kilo.jsonc`, so they agree without replacing a user's Kilo
configuration. Switching engines is a per-session decision, not a migration.

### LeanCTX context runtime

[LeanCTX](https://github.com/yvgude/lean-ctx) v3.9.3 is installed as a pinned,
SHA-256-bound native binary from the same offline dependency export as the engines.
Claude Code, OpenCode, Kilo, and the project Cursor configuration expose it in
MCP-only `minimal` mode (`ctx_read`, `ctx_search`, `ctx_glob`, `ctx_tree`, and
`ctx_shell`). This keeps tool-schema overhead small and avoids transparent shell
rewrites hiding release or failure evidence. Config, cache, session memory, and
indexes stay under `state/lean-ctx/`; update checks and autonomous features are
disabled. `ctx_shell` permits only argument-free `pwd`, `ls`, or `dir`; every
command with options or paths, plus builds, tests, Git, interpreters, and package
managers, keeps its native permission path. No global shell profile or user-level
editor configuration is modified.
Use `oracle ctx status`, `oracle ctx gain`, or `oracle ctx benchmark run .` to inspect
it. Do not run `lean-ctx setup` or `lean-ctx update` on the air-gapped appliance.

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

## Supporting UIs (all localhost)

- **`oracle ide`** — the Cursor-like IDE: **VSCodium** (telemetry-free VS Code — the
  same platform Cursor is built on) with **Continue** (codebase chat `Cmd+L`, inline
  edits `Cmd+I`, tab autocomplete on the fast lane, embeddings-backed codebase
  indexing) and **Kilo Code** (the agentic composer panel: plan/act, file edits,
  terminal — the maintained successor to the discontinued Roo Code, configured
  local-provider-only with telemetry and session sharing disabled) — all pointed
  at llama-swap, so every keystroke of AI runs on local models. `oracle ide
  install` sets it up from policy-bound cache exports in repo-owned application,
  extension, and user-data directories (configs generated, updates and telemetry
  off); `oracle ide` opens the repo without modifying canonical user settings.
- **Parallel agent tabs** — `Cmd+Shift+A` (`Ctrl+Shift+A` on Windows) opens a full
  engine session (Claude Code) as an editor tab; open as many as you want, side by
  side, like Cursor's agent tabs. `Cmd+Shift+Alt+A` opens the agent in its **own git
  worktree + branch** so parallel agents never collide — merge the branch when you
  like the result. `Cmd+Alt+O` opens an OpenCode tab; the Agents sidebar also offers
  **Kilo Code** tabs (the Kilo CLI — same models and `kilo.jsonc` as the side panel).
  Also available from the terminal `+` dropdown ("Oracle Agent" profiles).
- **Model authority detection** — every IDE launch runs `sync-models`, which validates
  policy-bound model snapshots against tracked revisions, include patterns, and
  independently supplied shard digests, then rewires
  Continue and Kilo Code through explicitly selected files under `state/generated/`,
  plus the engine tier maps
  (`serving/tiers.env`), and the opus/sonnet/haiku aliases to models that exist on
  this machine. Merely copying or downloading a GGUF never makes it loadable.
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

Full detail in [`docs/SECURITY.md`](docs/SECURITY.md). In brief:

- No Cursor, no hosted APIs, no accounts. Models, inference, data, and memory never
  leave the machine.
- The generated serving gateway and llama-swap bind only to `127.0.0.1`; `oracle
  verify` fails when listener evidence is missing or non-loopback. Optional services
  retain their own platform-scoped checks.
- Telemetry, error reporting, and auto-update are disabled for all engines. **Kilo Code
  ships as a hardened in-repo fork** ([`engines/kilo/HARDENING.md`](engines/kilo/HARDENING.md)):
  gateway, login, cloud sharing/ingest, remote relay, feedback, Sentry/PostHog/OpenTelemetry,
  update + marketplace checks, remote model discovery, external autocomplete, and remote
  config schemas are all defanged.
- **Default-deny egress** is opt-in and platform-specific. Inspection must show
  complete target coverage before Windows per-program rules are treated as effective;
  macOS uses a global pf anchor. Task development and verification do not activate or
  modify host firewall state.
- **Verify it:** `oracle audit` (full sweep — binds, kill-switches, secret hygiene,
  hardening presence; fails on any break), `oracle verify-egress` (empirical: internet
  blocked, loopback intact), `oracle doctor` (security-posture section).
- Network touches the machine in exactly two ways after bootstrap: envoy windows
  (fetch-only, allowlisted, quarantined, provenance-tracked) — and nothing else.
- Network acquisition is an explicit export/model-download phase. Installers consume
  only a policy-bound `incoming/dependency-cache/`, exact package locks, and trusted
  model revisions plus independently promoted shard identities; `make verify` proves
  the resulting stack works offline.

## Model ensemble (defaults, editable in `serving/models.manifest`)

| Model | Role | Quant | ~Size | Notes |
|---|---|---|---|---|
| Qwen3-Coder-480B-A35B | primary coder (sonnet) | UD-Q4_K_XL | 276 GB | manifest nominal context 131k |
| Kimi-K2-Thinking | deep reasoner (opus) | UD-Q2_K_XL | 381 GB | manifest nominal context 98k |
| DeepSeek-V3.2 | alt reasoner | UD-Q3_K_XL | 320 GB | swap-in alternative |
| Qwen3-Coder-30B-A3B | fast lane (haiku) | Q8_0 | 33 GB | subagents, background, always on |
| Qwen3-Embedding-4B | embeddings | Q8_0 | 5 GB | RAG/docs, always on |

Big-slot models are configured to swap; fast and embedding models are configured as
resident. Actual loaded backend, offloaded layers, warm state, and usable context are
runtime evidence, not conclusions drawn from these nominal declarations.
`serving/models.profile` (written by an installer when a reduced profile is selected)
controls which rows are active on a machine.

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
  operator appends the exact run/task/nonce challenge from
  `memory/PENDING-APPROVALS.json` to `memory/APPROVALS.md`; reports surface the queue.
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

The repository does not certify frontier parity, model quality, or a particular target
host. Those conclusions require benchmark results plus a target-host runtime report.
The harness addresses process failures such as goal drift, context loss, repeated dead
ends, and false completion; it does not turn inferred hardware capability or nominal
model context into measured serving performance.
