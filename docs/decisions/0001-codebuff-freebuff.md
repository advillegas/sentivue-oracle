# Decision 0001 — Codebuff / Freebuff: not as engines; mine the architecture

Date: 2026-07-11 · Status: DECIDED (operator may overturn) · Seed brain: G7 (pin
decisions by value), G10 (dependency pulse), C-series (context integrity)

## What they are (verified 2026-07-11)

- **Codebuff** — Apache-2.0 open-source TypeScript agent framework + CLI
  (CodebuffAI/codebuff, ~7.2k stars, active: last push 2026-07-10). Multi-agent
  architecture: file-picker → planner → editor → reviewer, plus an agent-template
  system and `@codebuff/sdk`. Model routing runs through **their managed cloud
  backend** (no BYOK as of mid-2026; models via their OpenRouter account).
- **Freebuff** — the free, **ad-supported** tier on the same platform (launched
  2026-03; npm `freebuff`, 17.5k weekly downloads; client repo is
  `freebuff-private`, i.e. NOT open). "No API keys" means *their* keys, *their*
  servers: every prompt and file-context batch transits codebuff.com and
  third-party model hosts (DeepSeek V4, Kimi K2.7, MiniMax M3, Gemini for file
  finding). Ads are injected into the CLI session.

## Decision

**Rejected as an engine or component.** Both are cloud-routed by construction —
code context leaves the machine on every request. That violates this platform's
constitution (fully offline after setup, no accounts, no hosted APIs, loopback-
only services), the same axis on which Cursor was excluded. Freebuff is
strictly worse: ad-supported monetization plus a closed client. No BYOK/local
endpoint exists to point at llama-swap, so there is no privacy-preserving
integration path even in principle.

## What we take anyway (Apache-2.0 ideas, not their infrastructure)

1. **Dedicated cheap file-picker pass.** Their biggest speed win is a small
   model that selects relevant files BEFORE the strong model thinks. Our
   `research=true` pre-pass partially covers this; a future mission could add a
   haiku-tier file-picker output (file list + one-line reasons) into every
   developer prompt, not just researched tasks.
2. **Agent templates as data.** Their agents are declarative templates with
   spawnable subagents — validation of our persona-file approach; worth
   revisiting if personas ever need per-mission specialization.
3. **`knowledge.md` convention** — knowledge stored alongside code; already
   covered here by skills/, AGENTS.md, and the seed brain. No action.

## Revisit when

- Codebuff ships true BYOK / self-hosted routing (watch their releases), or
- the open-source framework becomes runnable fully offline against an
  OpenAI-compatible endpoint without their backend.
