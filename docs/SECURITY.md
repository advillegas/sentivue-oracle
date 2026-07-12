# Security & privacy posture

SentiVue Oracle is an offline appliance. After bootstrap it runs with **no
account, no hosted API, no telemetry**, and — when hardened — **no outbound
network at all** except the loopback interface. This document is the map of
what enforces that and how to verify it.

## Threat model

The adversary is *silent exfiltration*: a vendored engine, extension, or
model-serving binary that phones home (telemetry, crash reports, "anonymous"
usage, license/update checks, cloud session sync, remote model catalogs) or that
a future upgrade quietly re-points at a new endpoint. We do not trust any
third-party binary to stay quiet just because a config flag asks it to.

## Defense in depth (three independent layers)

| Layer | Mechanism | Trust assumption |
|---|---|---|
| 1. Config | generated configs pin local-only providers, disable sharing/telemetry, remove remote schema URLs | vendor honors documented config |
| 2. Environment | `engines/kilo/hardened-env.*`, engine `settings.json`, VSCodium settings set every documented kill-switch | vendor honors documented env vars |
| 3. **Network (guarantee)** | **OS default-deny egress** (`bootstrap/harden-egress.*`) blocks all non-loopback traffic per process | **none — enforced by the OS, not the binary** |

Layers 1–2 are best-effort. Layer 3 is the guarantee: if a knob is ignored or an
upgrade adds an endpoint, the packets still cannot leave the machine.

## Default-deny egress

`oracle harden` applies an OS-level, per-process default-deny to every network-capable
process class the appliance runs. Loopback (`127.0.0.1`, `::1`) is exempt, so
local model serving and all local UIs keep working.

Covered process classes:

- **VSCodium** — main, extension host, and renderer processes
- **Agent engines** — Claude Code, OpenCode, Kilo Code
- **Inference servers** — llama-swap, llama-server
- **Agent-spawned package managers** — npm/npx, pip, uv/uvx
- **MCP servers** — node/python/uvx processes launched by agents
- **Containers launched by agents** — the Docker/Supabase stack

- **Windows** — `bootstrap/harden-egress.ps1` writes Windows Firewall outbound
  BLOCK rules per program image (group `SentiVue Oracle Egress`). Windows exempts
  loopback from firewall filtering, so localhost is unaffected automatically.
  Needs admin (the script self-elevates via UAC).
- **macOS/Linux** — `bootstrap/harden-egress.sh` loads a pf anchor that blocks
  all outbound except `lo0`. Because pf is global it is a strict superset of the
  per-process list above. Needs sudo.

```
oracle harden            # enable default-deny egress
oracle harden off        # remove it (e.g. before a deliberate download)
oracle egress status     # show rules + resolved program paths (plan)
oracle verify-egress     # empirical proof: internet blocked, loopback intact
```

The single controlled network path is the **envoy** (`oracle envoy`): it drops
the guard, performs fetch-only work against an allowlist into quarantine, and
restores the guard on exit (even on crash). Workers never get network regardless
of the guard state — their engine permission sets deny every network tool.

## Kilo hardening

Kilo Code is cloud-first upstream; we ship it as a contained, defanged fork.
Full detail and the request-by-request mapping is in
[`engines/kilo/HARDENING.md`](../engines/kilo/HARDENING.md); the evidence-derived
list of endpoints we neutralize is
[`engines/kilo/call-home-hosts.txt`](../engines/kilo/call-home-hosts.txt).
Disabled: Kilo Gateway, login/account, cloud session sharing + ingest, remote
relay/websockets/embedded web UI, feedback, Sentry/PostHog/OpenTelemetry, update
checks, marketplace, remote model discovery, external autocomplete/websearch,
remote config schemas, and endpoint fallback.

## Continuous verification

```
oracle audit             # full sweep: binds, telemetry, kill-switches, secrets
oracle audit --deep      # + re-scan the vendored Kilo binary's endpoints
oracle verify-egress     # empirical egress/loopback assertion
oracle doctor            # includes a security-posture section
```

`oracle audit` fails (nonzero) on any hard invariant break: a service bound off
loopback, a missing telemetry kill-switch, a denied-webfetch permission removed,
a remote schema re-introduced, the Kilo hardening layer gone, or a secret
pattern in a tracked file. WARNs are advisory (e.g. egress guard currently
toggled off).

## Re-verifying after a version bump

When any pinned engine/binary changes in `VERSIONS.lock`, re-derive the call-home
surface and re-run the sweep:

```
node bootstrap/scan-binary.mjs <path-to-binary> --hosts-only   # list endpoints
oracle audit --deep                                            # sweep + scan
oracle verify-egress                                           # prove the block
```

The egress default-deny needs no re-derivation — it blocks everything not on
loopback by construction, so new endpoints are covered automatically.
