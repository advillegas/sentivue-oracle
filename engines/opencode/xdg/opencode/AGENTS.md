# SentiVue Oracle — global rules (OpenCode engine)

The full operating doctrine is loaded from `engines/shared/CONVENTIONS.md` via the
`instructions` config — it governs model-tier etiquette, the memory ledger protocol,
worktree rules, and engineering standards.

Engine note: you are OpenCode running against local models via llama-swap
(`127.0.0.1:9099/v1`). The `oracle/*` model list in opencode.json is auto-generated
from what is installed on THIS machine; the default model is the sonnet tier and
`small_model` is the haiku tier (see `serving/tiers.env`). Big-slot swaps cost
minutes — batch your hardest questions rather than ping-ponging models.
This machine is intentionally air-gapped; webfetch/curl denials are by design.
