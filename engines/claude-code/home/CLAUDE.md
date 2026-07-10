# SentiVue Oracle — global memory (Claude Code engine)

@../../shared/CONVENTIONS.md
@../../shared/AUTONOMY.md

Engine note: you are Claude Code running against local models via llama-swap
(`127.0.0.1:9099`). Tier aliases opus/sonnet/haiku are remapped to the models
actually installed on THIS machine (see `serving/tiers.env`, auto-detected —
do not assume specific model names). The curl/wget/WebFetch denials are not an
obstacle to work around — this machine is intentionally air-gapped.
