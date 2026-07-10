#!/usr/bin/env python3
"""
oracle console — local mission-control web UI (127.0.0.1:8800, stdlib only).

One page, auto-refreshing: mission state, awaiting approvals (one-click APPROVE),
network request queue, ledger tail, reports, and links to the other surfaces
(llama-swap UI, Gitea vault UI). This is the oversight cockpit for the loop;
interactive coding sessions stay in the engines' own TUIs.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY, REPORTS = ROOT / "memory", ROOT / "reports"
PORT = 8800

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>SentiVue Oracle — console</title>
<meta http-equiv="refresh" content="30">
<style>
 body {{ font: 14px/1.5 -apple-system, Menlo, monospace; margin: 0; background:#0d1117; color:#c9d1d9; }}
 header {{ padding: 14px 24px; background:#161b22; border-bottom:1px solid #30363d; display:flex; gap:18px; align-items:baseline; }}
 header b {{ color:#e6edf3; font-size:16px; }}
 header a {{ color:#58a6ff; text-decoration:none; }}
 main {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:16px 24px; }}
 section {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 16px; overflow:auto; max-height:44vh; }}
 section.wide {{ grid-column: 1 / -1; }}
 h2 {{ margin:0 0 8px; font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:#8b949e; }}
 pre {{ margin:0; white-space:pre-wrap; word-break:break-word; }}
 form {{ display:inline; }}
 button {{ background:#238636; color:#fff; border:0; border-radius:6px; padding:3px 10px; cursor:pointer; }}
 .warn {{ color:#d29922; }} .ok {{ color:#3fb950; }}
 table {{ border-collapse:collapse; width:100%; }} td,th {{ text-align:left; padding:2px 8px 2px 0; vertical-align:top; }}
</style></head><body>
<header><b>SentiVue Oracle</b>
 <a href="http://127.0.0.1:9099" target="_blank">models (llama-swap)</a>
 <a href="http://127.0.0.1:3300" target="_blank">vault (gitea)</a>
 <a href="http://127.0.0.1:54323" target="_blank">supabase studio</a>
 <span style="margin-left:auto;color:#8b949e">refreshes every 30 s</span>
</header>
<main>
<section class="wide"><h2>Mission state</h2><pre>{state}</pre></section>
<section><h2>Awaiting operator approval</h2>{approvals}</section>
<section><h2>Network requests (for the envoy)</h2><pre>{netreq}</pre></section>
<section><h2>Ledger tail</h2><pre>{ledger}</pre></section>
<section><h2>Reports</h2>{reports}</section>
</main></body></html>"""


def read_tail(p: Path, lines: int) -> str:
    if not p.exists():
        return "(none yet)"
    return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def awaiting_ids() -> list[str]:
    state = MEMORY / "STATE.md"
    ids = []
    if state.exists():
        for m in re.finditer(r"\|\s*([\w-]+)\s*\|\s*pending.*awaiting operator approval",
                             state.read_text(encoding="utf-8", errors="replace")):
            ids.append(m.group(1))
    # Fallback: scan the ledger for APPROVAL NEEDED lines not yet approved.
    led = MEMORY / "LEDGER.md"
    if led.exists():
        for m in re.finditer(r"APPROVAL NEEDED ([\w-]+)", led.read_text(encoding="utf-8", errors="replace")):
            if m.group(1) not in ids:
                ids.append(m.group(1))
    approved = ""
    ap = MEMORY / "APPROVALS.md"
    if ap.exists():
        approved = ap.read_text(encoding="utf-8", errors="replace")
    return [i for i in ids if not re.search(rf"^APPROVE\s+{re.escape(i)}\s*$", approved, re.M)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: str, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/report/"):
            name = urllib.parse.unquote(self.path.split("/report/", 1)[1])
            p = REPORTS / Path(name).name           # no traversal
            body = p.read_text(encoding="utf-8", errors="replace") if p.exists() else "not found"
            return self._send(f"<pre style='font:13px Menlo;white-space:pre-wrap'>{html.escape(body)}</pre>")
        pending = awaiting_ids()
        if pending:
            rows = "".join(
                f"<tr><td><code>{html.escape(i)}</code></td><td>"
                f"<form method='post' action='/approve'>"
                f"<input type='hidden' name='id' value='{html.escape(i)}'>"
                f"<button>APPROVE</button></form></td></tr>" for i in pending)
            approvals = f"<table>{rows}</table><p class='warn'>Approving releases irreversible work — read the task first.</p>"
        else:
            approvals = "<p class='ok'>nothing waiting</p>"
        reports = "".join(
            f"<div><a style='color:#58a6ff' href='/report/{urllib.parse.quote(p.name)}'>{html.escape(p.name)}</a></div>"
            for p in sorted(REPORTS.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:15]
        ) or "(none yet)"
        self._send(PAGE.format(
            state=html.escape(read_tail(MEMORY / "STATE.md", 60)),
            approvals=approvals,
            netreq=html.escape(read_tail(MEMORY / "NET-REQUESTS.md", 30)),
            ledger=html.escape(read_tail(MEMORY / "LEDGER.md", 30)),
            reports=reports,
        ))

    def do_POST(self):
        if self.path != "/approve":
            return self._send("bad path", 404)
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        tid = (form.get("id") or [""])[0]
        if not re.match(r"^[\w-]{1,80}$", tid):
            return self._send("bad id", 400)
        ap = MEMORY / "APPROVALS.md"
        MEMORY.mkdir(exist_ok=True)
        with ap.open("a", encoding="utf-8") as f:
            f.write(f"APPROVE {tid}\n")
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


if __name__ == "__main__":
    print(f"oracle console: http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
