# Working rules for agents in this repository

Any agent working on this repo — IDE assistants, engine sessions, mission workers —
operates under `engines/shared/CONVENTIONS.md` and `engines/shared/AUTONOMY.md`.
The rules below are the distilled process failures from building this platform;
each one exists because violating it already cost real time here. Do not relearn
them.

1. **Diagnose from the artifact, not from your last change.** Read the actual log /
   stderr / API error body and reproduce in isolation before "fixing" anything.
   (The IDE extension "broken by branding" was actually a missing bundled binary,
   visible in the extension-host log the whole time.)

2. **Verify with production-shaped probes.** A 10-token hello proves a server is up,
   not that it works: agent sessions open with >25k tokens. Smoke tests must use
   realistic payloads. (Serving once passed hello and rejected every real session —
   context was split across parallel slots.)

3. **A config value you never saw take effect is a guess.** Unknown keys are ignored
   silently. After config changes, verify the observable: bound address, loaded
   model, real context size. (`listen:` in yaml was really a CLI flag; the server
   sat on 0.0.0.0:8080.)

4. **Missing tools are tasks, not blockers.** Install what you need, pinned
   (`bootstrap/ensure-tools.ps1|.sh` heals the core toolbelt). Never end a run with
   "X is not installed" as the reason. On the air-gapped node, queue a NET-REQUEST.

5. **Check dependency pulse at pin time.** Archived repo? Last release? Platform
   build present (universal VSIXes may lack native binaries)? List the remote's
   actual file layout before pinning an include pattern. (Roo Code was discontinued
   two months before it got installed here; a download pattern matched zero files.)

6. **Classify failures before counting them.** Infrastructure failures (API errors,
   dead endpoints, missing tools) are not task failures — refund the attempt, heal
   the platform, retry bounded. Recording them as work failures poisons the
   failure memory.

7. **Every capability ships as platform twins** (`.sh` + `.ps1`) or declares its
   platform explicitly and appears in the doctor. (Skills silently never synced on
   Windows for a day because only the bash symlink script existed.)

8. **Windows scripts: ASCII only, PowerShell 5.1 compatible, AST-parse-checked
   before commit** (`[System.Management.Automation.Language.Parser]::ParseFile`).
   Bash: `bash -n` before commit. Python: `py_compile`.

9. **No blobs in git.** 50 MB+ files never get committed (`bin/checkpoint` enforces
   this); models, artifacts, and toolchains live in gitignored dirs. History
   rewrites to undo a blob commit cost more than the guard ever will.

10. **Docs that describe machine state must be generated or point at the source of
    truth.** Hardcoded model names / tier maps in doctrine files become lies on the
    next machine and confuse small local models badly. (`serving/tiers.env` is the
    truth; `sync-models` regenerates everything downstream.)

11. **Incidents end in guards — and generalizable ones end in principles.** Every
    root-caused failure adds a mechanical check (doctor line, conductor rule,
    pre-commit guard, test) before it is closed. If the lesson generalizes beyond
    this repository, append it to `engines/shared/SEED-BRAIN.md` under NEW
    PRINCIPLES (next free ID in its series, `[strong]`, failure kernel kept,
    project specifics removed) — founding memory grows from incidents; a fix
    without a guard is a bug scheduled to return.

12. **Commit and push after every meaningful change** — to origin and to the local
    vault. The ledger (`memory/LEDGER.md`) records what and why.

13. **Keep a session journal.** For any multi-step task, maintain
    `memory/sessions/<session>.md` (DOING / DONE / NEXT / NOTES) and update it after
    every meaningful step. Claude Code sessions get one automatically via hooks
    (`bin/session-journal.js`); other surfaces create their own. After a compaction
    or restart, the journal outranks recall.
