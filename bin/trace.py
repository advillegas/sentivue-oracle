#!/usr/bin/env python3
"""trace - query CLI over engine session logs (decision 0003).

Every conductor engine run writes a stream-json transcript to logs/*.log:
one JSON event per line (LLM messages, tool calls, tool results, timings,
tokens). This is the platform's agent-behavior trace store; trace is its
query surface.

  python bin/trace.py list [--mission M]      session inventory with outcomes
  python bin/trace.py show <log>              tool-call tree with timings
  python bin/trace.py diff <a> <b>            attempt-over-attempt comparison
  python bin/trace.py grep <pattern> [--mission M]   search across sessions

Stdlib only. Malformed lines are skipped (logs may interleave non-JSON noise).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def read_events(path: Path) -> list[dict]:
    events = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type"):
            events.append(obj)
    return events


@dataclass
class Summary:
    events: int = 0
    turns: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    out_tokens: int = 0
    outcome: str = "?"

    @property
    def tools_line(self) -> str:
        return ", ".join(f"{k}x{v}" for k, v in sorted(self.tools.items())) or "-"


def summarize(events: list[dict]) -> Summary:
    s = Summary(events=len(events))
    for ev in events:
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            s.turns += 1
            usage = msg.get("usage") or {}
            s.out_tokens += int(usage.get("output_tokens") or 0)
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    name = c.get("name") or "tool"
                    s.tools[name] = s.tools.get(name, 0) + 1
                    s.tool_calls += 1
        elif ev.get("type") == "result":
            sub = ev.get("subtype") or "?"
            s.outcome = f"{sub}{'/error' if ev.get('is_error') else ''}"
    return s


def log_files(mission: str | None) -> list[Path]:
    if not LOGS.exists():
        return []
    files = sorted(LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if mission:
        files = [f for f in files if f.name.startswith(mission)]
    return files


def resolve_log(name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    p = LOGS / name
    if p.exists():
        return p
    if not name.endswith(".log") and (LOGS / f"{name}.log").exists():
        return LOGS / f"{name}.log"
    sys.exit(f"log not found: {name}")


# ---- commands -------------------------------------------------------------------

def cmd_list(mission: str | None) -> int:
    files = log_files(mission)
    if not files:
        print("no session logs found")
        return 0
    print(f"{'session':<52} {'events':>6} {'turns':>5} {'tools':>5} "
          f"{'out-tok':>8}  outcome")
    for f in files:
        s = summarize(read_events(f))
        print(f"{f.stem[:52]:<52} {s.events:>6} {s.turns:>5} {s.tool_calls:>5} "
              f"{s.out_tokens:>8}  {s.outcome}")
    return 0


def tool_target(inp: dict | None) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "pattern", "prompt", "url"):
        if inp.get(key):
            return str(inp[key]).replace("\n", " ")[:90]
    return ""


def cmd_show(name: str) -> int:
    events = read_events(resolve_log(name))
    results = {}
    for ev in events:
        if ev.get("type") == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    results[c.get("tool_use_id")] = bool(c.get("is_error"))
    turn = 0
    for ev in events:
        et = ev.get("type")
        if et == "system" and ev.get("subtype") == "init":
            print(f"[init] model={ev.get('model')} cwd={ev.get('cwd')}")
        elif et == "assistant":
            turn += 1
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and (c.get("text") or "").strip():
                    print(f"turn {turn}: {c['text'].strip()[:80]!r}")
                elif c.get("type") == "thinking":
                    print(f"turn {turn}: (thinking, {len(c.get('thinking') or '')} chars)")
                elif c.get("type") == "tool_use":
                    status = results.get(c.get("id"))
                    mark = "?" if status is None else ("ERR" if status else "ok")
                    print(f"    -> {c.get('name')}({tool_target(c.get('input'))}) [{mark}]")
        elif et == "result":
            secs = round(int(ev.get("duration_ms") or 0) / 1000)
            print(f"[result] {ev.get('subtype')} error={ev.get('is_error')} "
                  f"turns={ev.get('num_turns')} {secs}s")
    return 0


def cmd_diff(a: str, b: str) -> int:
    sa, sb = (summarize(read_events(resolve_log(x))) for x in (a, b))
    print(f"{'':<12} {'A':>10} {'B':>10}")
    for label, va, vb in [("events", sa.events, sb.events),
                          ("turns", sa.turns, sb.turns),
                          ("tool calls", sa.tool_calls, sb.tool_calls),
                          ("out tokens", sa.out_tokens, sb.out_tokens)]:
        print(f"{label:<12} {va:>10} {vb:>10}")
    print(f"{'outcome':<12} {sa.outcome:>10} {sb.outcome:>10}")
    names = sorted(set(sa.tools) | set(sb.tools))
    for n in names:
        ca, cb = sa.tools.get(n, 0), sb.tools.get(n, 0)
        if ca != cb:
            print(f"tool {n:<20} {ca:>5} vs {cb}")
    only_a = set(sa.tools) - set(sb.tools)
    only_b = set(sb.tools) - set(sa.tools)
    if only_a:
        print("only in A:", ", ".join(sorted(only_a)))
    if only_b:
        print("only in B:", ", ".join(sorted(only_b)))
    return 0


def cmd_grep(pattern: str, mission: str | None) -> int:
    rx = re.compile(pattern)
    hits = 0
    for f in log_files(mission):
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            m = rx.search(line)
            if m:
                start = max(0, m.start() - 20)
                print(f"{f.name}:{i}: {line[start:start + 120]}")
                hits += 1
                if hits >= 200:
                    print("... (capped at 200 hits)")
                    return 0
    if not hits:
        print("no matches")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="trace")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--mission", default=None)
    p = sub.add_parser("show")
    p.add_argument("log")
    p = sub.add_parser("diff")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("grep")
    p.add_argument("pattern")
    p.add_argument("--mission", default=None)
    args = ap.parse_args()
    if args.cmd == "list":
        return cmd_list(args.mission)
    if args.cmd == "show":
        return cmd_show(args.log)
    if args.cmd == "diff":
        return cmd_diff(args.a, args.b)
    return cmd_grep(args.pattern, args.mission)


if __name__ == "__main__":
    sys.exit(main())
