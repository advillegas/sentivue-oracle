# Decision 0005 — Hardened Kilo fork + OS default-deny egress

Date: 2026-07-11 · Status: ADOPTED · Seed brain: E16 (don't trust binaries to
behave), G10 (pulse the artifact), V-privacy (offline is a guarantee, not a
setting)

## Context

Kilo Code became a first-class engine (decision unrecorded; see LEDGER
2026-07-11) via the pinned `@kilocode/cli` binary. Kilo is cloud-first upstream:
account/gateway, 500+ hosted models, cloud session sharing/relay, PostHog +
Sentry + OpenTelemetry, update/marketplace checks, remote model discovery. None
of that belongs in an offline appliance. The prior posture disabled some of it
via config/env, but that trusts a 140 MB compiled Bun binary to honor flags —
and a version bump could silently re-point at a new endpoint.

## Decision

Ship Kilo as a **contained, defanged in-repo fork** and back it with an
**OS-level default-deny egress** that trusts no binary.

1. **Do not recompile.** Rebuilding the Bun single-file binary offline is not
   tractable and yields an unpinnable artifact. We contain the stock binary
   instead (evidence-derived; the binary was string-scanned to enumerate its
   real endpoints and env knobs — see `engines/kilo/call-home-hosts.txt`).
2. **Three independent layers** (config, environment, network) so no single
   bypass re-enables a call-home path. Detail in `engines/kilo/HARDENING.md`.
3. **The network layer is the guarantee.** `bootstrap/harden-egress.{ps1,sh}`
   applies a per-process default-deny to VSCodium, extension hosts, agent
   engines, inference servers, agent-spawned package managers, MCP servers, and
   agent-launched containers. Loopback stays up (Windows exempts it from WFP;
   macOS pf skips `lo0`), so local models keep working while the internet does
   not. Toggle-able; the envoy window is the sole controlled network path.
4. **Continuous verification.** `bootstrap/security-audit.{ps1,sh}` sweeps the
   privacy invariants (binds, kill-switches, secret hygiene, hardening presence)
   and fails CI-style on any break; `bootstrap/verify-egress.{ps1,sh}` proves the
   block empirically; `oracle doctor` gained a security-posture section.

## Alternatives considered

- **Config/env only** — rejected as the *sole* control: depends on vendor good
  behavior and silently regresses on upgrade.
- **Recompiled fork from `Kilo-Org/kilocode`** — rejected: not offline-buildable
  here; unpinnable; large maintenance surface. Left as a future option if we
  ever need to remove code paths rather than just block their egress.
- **Drop Kilo** — rejected: the user wants it as an engine and IDE panel; the
  hardening makes it safe to keep.

## Consequences

- Windows egress hardening needs admin (self-elevates via UAC); macOS needs sudo.
- With the guard ON, agent-spawned installs (npm/pip/uv) and container pulls are
  blocked — deliberate. Turn it off for a known download or use the envoy.
- After any `KILO_CLI_NPM_VERSION` bump, re-scan (`bootstrap/scan-binary.mjs`)
  and re-run `oracle audit --deep`; the egress default-deny covers new endpoints
  automatically.
