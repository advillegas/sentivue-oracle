# SentiVue Oracle — global rules (OpenCode engine)

The full operating doctrine is loaded from `engines/shared/CONVENTIONS.md` via the
`instructions` config — it governs model-tier etiquette, the memory ledger protocol,
worktree rules, and engineering standards.

Engine note: you are OpenCode running against local models via llama-swap
(`127.0.0.1:9099/v1`). Use `oracle/qwen3-coder-480b` for real work,
`oracle/qwen3-coder-30b` for grunt work, `oracle/kimi-k2-thinking` for architecture
and hard debugging (big-slot swaps cost 1–2 minutes — batch those questions).
This machine is intentionally air-gapped; webfetch/curl denials are by design.
