/* Webview side of the agent conversation - Cursor-style transcript:
 * assistant prose flows plainly; thinking and tool calls render as slim
 * expandable activity rows ("Thought for 12s", "Ran terminal", "Edited file")
 * that stream in real time; turn metadata is small and quiet. */
"use strict";
const vscode = acquireVsCodeApi();
const chat = document.getElementById("chat");
const col = document.getElementById("col");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const statusDot = document.getElementById("dot");
const statusTxt = document.getElementById("status");

let busy = false;
let stream = null;        // per-message streaming state: {wrap, blocks:{index->st}}
const toolCards = {};     // tool_use_id -> row element

function setBusy(b, label) {
  busy = b;
  sendBtn.disabled = b;
  stopBtn.disabled = !b;
  statusDot.className = b ? "busy" : "ready";
  statusDot.id = "dot";
  statusTxt.textContent = label || (b ? "working…" : "ready");
}

function scrolledToBottom() {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
}
function autoscroll(force) {
  if (force || scrolledToBottom()) chat.scrollTop = chat.scrollHeight;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/* minimal safe markdown: text runs + ``` fences */
function renderMarkdown(target, text) {
  target.textContent = "";
  const parts = String(text).split(/```(\w*)\n?([\s\S]*?)(?:```|$)/g);
  for (let i = 0; i < parts.length; i += 3) {
    if (parts[i]) {
      const p = el("div", "text-run");
      p.textContent = parts[i].replace(/\n{3,}/g, "\n\n");
      target.appendChild(p);
    }
    if (i + 2 < parts.length && parts[i + 2] !== undefined) {
      const pre = el("pre");
      pre.textContent = parts[i + 2];
      target.appendChild(pre);
    }
  }
}

function addUser(text) {
  const t = el("div", "turn");
  t.appendChild(el("div", "user-msg", text));
  col.appendChild(t);
  autoscroll(true);
}

function addSys(text) {
  col.appendChild(el("div", "sysline", text));
  autoscroll();
}

/* ---------- activity rows ---------- */

function row(cls, glyph, verb, target) {
  const d = document.createElement("details");
  d.className = "row " + cls;
  const s = el("summary");
  s.appendChild(el("span", "glyph", glyph));
  s.appendChild(el("span", "verb", verb));
  s.appendChild(el("span", "target", target || ""));
  s.appendChild(el("span", "state", ""));
  d.appendChild(s);
  d.appendChild(el("div", "body", ""));
  return d;
}

const TOOL_VERBS = {
  Bash:        (i) => ["$", "Ran terminal", i && i.command ? i.command : ""],
  Write:       (i) => ["✎", "Wrote file", i && i.file_path ? rel(i.file_path) : ""],
  Edit:        (i) => ["✎", "Edited file", i && i.file_path ? rel(i.file_path) : ""],
  NotebookEdit:(i) => ["✎", "Edited notebook", i && i.notebook_path ? rel(i.notebook_path) : ""],
  Read:        (i) => ["◇", "Read file", i && i.file_path ? rel(i.file_path) : ""],
  Grep:        (i) => ["◎", "Searched code", i && i.pattern ? i.pattern : ""],
  Glob:        (i) => ["◎", "Searched files", i && (i.glob_pattern || i.pattern) || ""],
  Task:        (i) => ["❖", "Launched subagent", i && (i.subagent_type || i.description) || ""],
  TodoWrite:   () => ["☰", "Updated plan", ""],
  WebFetch:    (i) => ["↓", "Fetched", i && i.url || ""],
};
function rel(p) {
  return String(p).replace(/\\/g, "/").split("/").slice(-3).join("/");
}
function toolMeta(name, input) {
  const fn = TOOL_VERBS[name];
  if (fn) { try { return fn(input); } catch { /* fall through */ } }
  if (name && name.startsWith("mcp__")) return ["⌁", name.replace(/^mcp__/, "").replace(/__/g, " · "), ""];
  return ["·", name || "tool", ""];
}

function toolRow(name, input) {
  const [glyph, verb, target] = toolMeta(name, input);
  const d = row("tool", glyph, verb, target);
  d.querySelector(".state").classList.add("run");
  return d;
}

function updateToolRow(d, name, input) {
  const [glyph, verb, target] = toolMeta(name, input);
  d.querySelector(".glyph").textContent = glyph;
  d.querySelector(".verb").textContent = verb;
  if (target) d.querySelector(".target").textContent = target;
  const detail = (input && typeof input === "object")
    ? JSON.stringify(input, null, 1) : String(input || "");
  d.querySelector(".body").textContent = detail.slice(0, 3000);
}

function thinkingRow() {
  const d = row("thinking", "✳", "Thinking", "");
  d.open = true;                     // live view while streaming
  d.querySelector(".state").classList.add("run");
  d._t0 = Date.now();
  return d;
}
function finishThinking(d) {
  if (!d) return;
  const secs = Math.max(1, Math.round((Date.now() - (d._t0 || Date.now())) / 1000));
  d.querySelector(".verb").textContent = `Thought for ${secs}s`;
  const st = d.querySelector(".state");
  st.classList.remove("run"); st.textContent = "";
  d.open = false;                    // collapse; click to expand
}

function ensureStream() {
  if (stream) return stream;
  const wrap = el("div", "turn");
  col.appendChild(wrap);
  stream = { wrap, blocks: {} };
  return stream;
}

/* ---------- event handling ---------- */

function handleStreamEvent(ev) {
  const s = ensureStream();
  if (ev.type === "content_block_start") {
    const cb = ev.content_block || {};
    if (cb.type === "thinking") {
      const d = thinkingRow();
      s.blocks[ev.index] = { kind: "thinking", elem: d, buf: "" };
      s.wrap.appendChild(d);
      setBusy(true, "thinking…");
    } else if (cb.type === "text") {
      const t = el("div", "text-block");
      s.blocks[ev.index] = { kind: "text", elem: t, buf: "" };
      s.wrap.appendChild(t);
      setBusy(true, "writing…");
    } else if (cb.type === "tool_use") {
      const card = toolRow(cb.name, null);
      s.blocks[ev.index] = { kind: "tool", elem: card, buf: "", name: cb.name, id: cb.id };
      if (cb.id) toolCards[cb.id] = card;
      s.wrap.appendChild(card);
      setBusy(true, (toolMeta(cb.name, null)[1] || "tool").toLowerCase() + "…");
    }
    autoscroll();
  } else if (ev.type === "content_block_delta") {
    const b = s.blocks[ev.index];
    if (!b) return;
    const d = ev.delta || {};
    if (d.type === "thinking_delta" && b.kind === "thinking") {
      b.buf += d.thinking || "";
      b.elem.querySelector(".body").textContent = b.buf;
    } else if (d.type === "text_delta" && b.kind === "text") {
      b.buf += d.text || "";
      renderMarkdown(b.elem, b.buf);
    } else if (d.type === "input_json_delta" && b.kind === "tool") {
      b.buf += d.partial_json || "";
      let parsed = null;
      try { parsed = JSON.parse(b.buf); } catch { /* incomplete json */ }
      if (parsed) updateToolRow(b.elem, b.name, parsed);
    }
    autoscroll();
  } else if (ev.type === "content_block_stop") {
    const b = s.blocks[ev.index];
    if (b && b.kind === "thinking") finishThinking(b.elem);
    if (b && b.kind === "tool" && b.buf) {
      try { updateToolRow(b.elem, b.name, JSON.parse(b.buf)); } catch { /* raw */ }
    }
  }
}

function handleAssistant(msg) {
  const s = ensureStream();
  const streamed = Object.keys(s.blocks).length > 0;
  for (const c of msg.content || []) {
    if (c.type === "thinking" && !streamed) {
      const d = thinkingRow();
      d.querySelector(".body").textContent = c.thinking || "";
      finishThinking(d);
      s.wrap.appendChild(d);
    } else if (c.type === "text" && !streamed) {
      const t = el("div", "text-block");
      renderMarkdown(t, c.text || "");
      s.wrap.appendChild(t);
    } else if (c.type === "tool_use") {
      let card = c.id && toolCards[c.id];
      if (!card) {
        card = toolRow(c.name, c.input);
        if (c.id) toolCards[c.id] = card;
        s.wrap.appendChild(card);
      }
      updateToolRow(card, c.name, c.input);
    }
  }
  stream = null;   // message boundary
  autoscroll();
}

function handleToolResult(c) {
  const card = toolCards[c.tool_use_id];
  if (!card) return;
  let out = "";
  if (typeof c.content === "string") out = c.content;
  else if (Array.isArray(c.content)) {
    out = c.content.map((x) => (x && x.type === "text" ? x.text : "")).join("\n");
  }
  const body = card.querySelector(".body");
  const outEl = el("div", "tool-out" + (c.is_error ? " err" : ""));
  outEl.textContent = (out || "(no output)").slice(0, 6000);
  body.appendChild(outEl);
  const st = card.querySelector(".state");
  st.classList.remove("run");
  st.classList.add(c.is_error ? "err" : "ok");
  st.textContent = c.is_error ? "✗" : "✓";
  autoscroll();
}

function fmtTokens(u) {
  if (!u) return "";
  const inTok = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
  const k = (n) => n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  return `${k(inTok)} in · ${k(u.output_tokens || 0)} out`;
}

window.addEventListener("message", (e) => {
  const ev = e.data;
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init") {
        document.getElementById("model").textContent = ev.model || "local";
        setBusy(false);
      }
      break;
    case "stream_event": handleStreamEvent(ev.event || {}); break;
    case "assistant": handleAssistant(ev.message || {}); break;
    case "user":
      for (const c of (ev.message && ev.message.content) || []) {
        if (c && c.type === "tool_result") handleToolResult(c);
      }
      break;
    case "result": {
      col.appendChild(el("div", "turn-meta",
        `${Math.round((ev.duration_ms || 0) / 1000)}s · ${fmtTokens(ev.usage)}`));
      stream = null;
      setBusy(false);
      autoscroll();
      break;
    }
    case "proc-exit":
      addSys(`engine exited (${ev.code}) — Restart spawns a fresh session`);
      setBusy(false, "exited");
      break;
    case "proc-error":
      addSys("engine: " + ev.text);
      break;
    case "echo-user":
      addUser(ev.text);
      setBusy(true, "queued…");
      break;
  }
});

function send() {
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  input.style.height = "auto";
  vscode.postMessage({ type: "send", text });
}
sendBtn.addEventListener("click", send);
stopBtn.addEventListener("click", () => vscode.postMessage({ type: "stop" }));
document.getElementById("restart").addEventListener("click", () => vscode.postMessage({ type: "restart" }));
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
});
setBusy(false, "starting engine…");
vscode.postMessage({ type: "ready" });
