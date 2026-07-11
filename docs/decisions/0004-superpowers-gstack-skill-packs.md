# Decision 0004 — Adopt superpowers + gstack as vendored skill packs

Date: 2026-07-11 · Status: ADOPTED · Seed brain: G10 (pulse), C5 (load
selectively), G9 (descriptions trigger full text)

## Pulse (verified 2026-07-11)

- **obra/superpowers** — MIT, 252k stars, pushed same-day, latest tag v6.1.1.
  Methodology-as-code: 14 composable skills enforcing brainstorm → worktree →
  plan → TDD (watch the test fail) → subagent execution with two-stage review,
  plus four-phase systematic debugging and skill-authoring. Substantially
  convergent with our AUTONOMY protocol and seed brain (V2, A8, E1) — which is
  evidence for both, and its checklists are more granular than ours.
- **garrytan/gstack** — MIT, 121k stars, pushed same-day, no tags (pinned by
  commit SHA). 23 role specialists as skills: /office-hours product thinking,
  plan-ceo/eng/design/devex reviews, /review, /qa, /ship, /retro, /investigate,
  /careful, /freeze-/guard-/unfreeze change control. Markdown + small scripts;
  runs wherever the engine runs.

## How they are integrated

- Vendored pinned under `harness/skill-packs/vendor/` (gitignored), pins in
  `VERSIONS.lock`, installer twins `install-skill-packs.ps1|.sh` wired into
  `oracle.ps1 setup` and `bootstrap/install.sh`.
- Synced into BOTH engines with prefixes: `sp-*` (14 skills), `gs-*` (53
  skills). Progressive disclosure applies — idle skills cost only their
  name+description line (G9/C5).
- They run on OUR engines against OUR local models. gstack/superpowers skills
  that assume external services (deploy targets, hosted browsers, gbrain sync)
  simply go unused; nothing phones home by construction — skills are inert
  markdown until an engine follows them.

## Privacy note

Both are instruction sets, not services. The only executable surface is
gstack's helper scripts (bin/, browse tooling); those run locally and only when
a skill is explicitly followed. The air-gap/firewall posture is unaffected.

## Known cost + curation lever

78 skill descriptions now ride in every engine session's skill index (name +
description each). If the context tax or trigger confusion becomes measurable
(retro metric: wrong-skill loads, prompt-size growth), curate like ECC:
a profile.txt allowlist in the installer is the designed-in lever. Full packs
were installed deliberately per operator instruction 2026-07-11.

## Overlap map (who wins when rules collide)

Platform doctrine outranks pack skills (G5: owner > project rules > packs).
Overlaps: superpowers TDD/debugging ≈ AUTONOMY §5/§6 + E1 (compatible,
packs add granularity); gstack /review + /qa ≈ auditor/adversary personas
(packs are INTERACTIVE-session tools; mission verification stays with the
conductor's independent audit stack); gstack /freeze//guard ≈ approvals
(complementary — packs gate the session, APPROVALS.md gates the mission).
