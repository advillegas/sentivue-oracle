#!/usr/bin/env node
/* session-journal.js - self-referential per-session memory.
 *
 * Every agent session gets memory/sessions/<session-id>.md: what it is DOING,
 * what is DONE, what is NEXT. Claude Code hooks call this script so the journal
 * is mechanically re-injected at every session start, resume, and post-compaction
 * restart - the agent never loses the thread even when its context does.
 *
 *   node bin/session-journal.js start     SessionStart hook: ensure journal,
 *                                         emit it (+ STATE.md tail) as context
 *   node bin/session-journal.js compact   PreCompact hook: mark the break
 *   node bin/session-journal.js end       SessionEnd hook: close the journal
 *
 * Reads the hook payload (session_id, source, cwd) from stdin JSON.
 * Dependency-free; works on Windows and macOS. Failures never block a session.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const MODE = process.argv[2] || "start";
const ROOT = process.env.ORACLE_ROOT || path.dirname(__dirname);
const DIR = path.join(ROOT, "memory", "sessions");
const KEEP = 30; // most recent journals kept; older ones pruned

function readStdin() {
  try {
    const raw = fs.readFileSync(0, "utf8");
    return raw.trim() ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function tail(file, chars) {
  try {
    const txt = fs.readFileSync(file, "utf8");
    return txt.length > chars ? "..." + txt.slice(-chars) : txt;
  } catch {
    return "";
  }
}

function prune() {
  try {
    const files = fs.readdirSync(DIR)
      .filter((f) => f.endsWith(".md"))
      .map((f) => ({ f, t: fs.statSync(path.join(DIR, f)).mtimeMs }))
      .sort((a, b) => b.t - a.t);
    for (const { f } of files.slice(KEEP)) fs.unlinkSync(path.join(DIR, f));
  } catch { /* best effort */ }
}

function main() {
  const payload = readStdin();
  const sid = String(payload.session_id || `manual-${Date.now()}`)
    .replace(/[^\w.-]/g, "_").slice(0, 64);
  const source = payload.source || payload.trigger || "";
  const cwd = payload.cwd || process.cwd();
  fs.mkdirSync(DIR, { recursive: true });
  const journal = path.join(DIR, `${sid}.md`);
  const rel = path.relative(cwd, journal) || journal;

  if (MODE === "compact") {
    if (fs.existsSync(journal)) {
      fs.appendFileSync(journal,
        `\n> ${ts()} - CONTEXT COMPACTED. Re-read this journal; trust it over recall.\n`);
    }
    process.exit(0);
  }

  if (MODE === "end") {
    if (fs.existsSync(journal)) {
      fs.appendFileSync(journal, `\n---\nended: ${ts()} (${source || "session end"})\n`);
    }
    process.exit(0);
  }

  // ---- start ----------------------------------------------------------------
  let fresh = false;
  if (!fs.existsSync(journal)) {
    fresh = true;
    fs.writeFileSync(journal,
`# Session journal - ${sid}
started: ${ts()} | source: ${source || "startup"} | cwd: ${cwd}

Maintain this file YOURSELF as you work - it is your continuity across
compaction, crashes, and restarts. After every meaningful step: move finished
items to DONE, restate DOING in one line, keep NEXT current. If this file is
stale, you are flying blind.

## DOING (one line, current objective)
- (not yet stated)

## DONE (this session)

## NEXT / TODO

## NOTES / OPEN QUESTIONS
`);
  }
  prune();

  const parts = [];
  parts.push(`SESSION JOURNAL (${fresh ? "new" : "restored"} - maintain it at: ${rel})`);
  parts.push(tail(journal, 3000));
  const state = tail(path.join(ROOT, "memory", "STATE.md"), 1200);
  if (state) parts.push(`\nPLATFORM STATE (memory/STATE.md tail):\n${state}`);
  const lessons = tail(path.join(ROOT, "memory", "LESSONS.md"), 800);
  if (lessons) parts.push(`\nLESSONS (do not relearn):\n${lessons}`);
  parts.push(`\nRULE: update ${rel} after every meaningful step (DOING / DONE / NEXT).`);

  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: parts.join("\n"),
    },
  }));
}

try { main(); } catch { process.exit(0); } // journals must never break a session
