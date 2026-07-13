#!/usr/bin/env python3
"""
oracle console — local mission-control web UI (127.0.0.1:8800, stdlib only).

One page, auto-refreshing: mission state, awaiting decisions (APPROVE or DENY),
network request queue, ledger tail, reports, and links to the other surfaces
(llama-swap UI, Gitea vault UI). This is the oversight cockpit for the loop;
interactive coding sessions stay in the engines' own TUIs.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
 button.deny {{ background:#da3633; }}
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


def pending_approvals() -> list[dict[str, Any]]:
    path = MEMORY / "PENDING-APPROVALS.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 2
        or not isinstance(payload.get("pending"), list)
    ):
        return []
    records: list[dict[str, Any]] = []
    for raw in payload["pending"]:
        if not isinstance(raw, dict):
            continue
        run_id = raw.get("run_id")
        task_id = raw.get("task_id")
        generation = raw.get("generation")
        task_digest = raw.get("task_digest")
        nonce = raw.get("nonce")
        command = raw.get("command")
        deny_command = raw.get("deny_command")
        expected = (
            f"APPROVE {run_id} {task_id} {generation} "
            f"{task_digest} {nonce}"
        )
        expected_denial = (
            f"DENY {run_id} {task_id} {generation} "
            f"{task_digest} {nonce}"
        )
        if (
            isinstance(run_id, str)
            and re.fullmatch(r"[0-9a-f]{32}", run_id)
            and isinstance(task_id, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", task_id)
            and isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation >= 1
            and isinstance(task_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", task_digest)
            and isinstance(nonce, str)
            and len(nonce) >= 24
            and command == expected
            and deny_command == expected_denial
        ):
            records.append(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "generation": generation,
                    "task_digest": task_digest,
                    "nonce": nonce,
                    "command": command,
                    "deny_command": deny_command,
                }
            )
    approved: set[str] = set()
    ap = MEMORY / "APPROVALS.md"
    if ap.exists():
        approved = {
            line.strip()
            for line in ap.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        }
    return [
        record
        for record in records
        if record["command"] not in approved
        and record["deny_command"] not in approved
    ]


APPROVAL_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: str, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/report/"):
            name = urllib.parse.unquote(self.path.split("/report/", 1)[1])
            p = REPORTS / Path(name).name           # no traversal
            body = p.read_text(encoding="utf-8", errors="replace") if p.exists() else "not found"
            return self._send(f"<pre style='font:13px Menlo;white-space:pre-wrap'>{html.escape(body)}</pre>")
        pending = pending_approvals()
        if pending:
            def decision_form(item: dict[str, Any], decision: str) -> str:
                style = " class='deny'" if decision == "DENY" else ""
                return (
                    "<form method='post' action='/decision'>"
                    f"<input type='hidden' name='decision' value='{decision}'>"
                    f"<input type='hidden' name='run_id' "
                    f"value='{html.escape(item['run_id'])}'>"
                    f"<input type='hidden' name='id' "
                    f"value='{html.escape(item['task_id'])}'>"
                    f"<input type='hidden' name='generation' "
                    f"value='{item['generation']}'>"
                    f"<input type='hidden' name='task_digest' "
                    f"value='{html.escape(item['task_digest'])}'>"
                    f"<input type='hidden' name='nonce' "
                    f"value='{html.escape(item['nonce'])}'>"
                    f"<button{style}>{decision}</button></form>"
                )

            rows = "".join(
                f"<tr><td><code>{html.escape(item['task_id'])}</code></td><td>"
                f"{decision_form(item, 'APPROVE')} "
                f"{decision_form(item, 'DENY')}</td></tr>"
                for item in pending
            )
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
        if self.path != "/decision":
            return self._send("bad path", 404)
        if self.headers.get("Origin") not in {
            f"http://127.0.0.1:{PORT}",
            f"http://localhost:{PORT}",
        }:
            return self._send("bad origin", 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send("bad length", 400)
        if not 0 < length <= 4096:
            return self._send("bad length", 400)
        try:
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", errors="strict")
            )
        except UnicodeDecodeError:
            return self._send("bad encoding", 400)
        run_id = (form.get("run_id") or [""])[0]
        tid = (form.get("id") or [""])[0]
        generation = (form.get("generation") or [""])[0]
        task_digest = (form.get("task_digest") or [""])[0]
        nonce = (form.get("nonce") or [""])[0]
        decision = (form.get("decision") or [""])[0]
        if decision not in {"APPROVE", "DENY"}:
            return self._send("invalid decision", 400)
        command = (
            f"{decision} {run_id} {tid} {generation} "
            f"{task_digest} {nonce}"
        )
        command_field = "command" if decision == "APPROVE" else "deny_command"
        if not any(
            record[command_field] == command
            for record in pending_approvals()
        ):
            return self._send("operator challenge is invalid or stale", 403)
        ap = MEMORY / "APPROVALS.md"
        MEMORY.mkdir(exist_ok=True)
        with APPROVAL_LOCK:
            descriptor = os.open(
                ap,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                remaining = memoryview((command + "\n").encode("utf-8"))
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("approval append made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


if __name__ == "__main__":
    print(f"oracle console: http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
