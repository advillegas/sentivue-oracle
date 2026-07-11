/* Webview side of the agent conversation: renders Claude Code's stream-json
 * events as a Cursor-style transcript - live thinking (click to expand),
 * collapsible tool cards with inputs/outputs, per-turn usage metadata. */
"use strict";
const vscode = acquireVsCodeApi();
const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const statusDot = document.getElementById("dot");
const statusTxt = document.getElementById("status");

let busy = false;
let stream = null;        // per-message streaming state: {wrap, blocks: {index -> el}}
const toolCards = {};     // tool_use_id -> card element

function setBusy(b, label) {
  busy = b;
  sendBtn.disabled = b;
  stopBtn.disabled = !b;
  statusDot.className = "dot " + (b ? "busy" : "ready");
  statusTxt.textContent = label || (b ? "working…" : "ready");
}

function scrolledToBottom() {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
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

// minimal safe markdown: escape, then fence blocks + inline code + newlines
function renderMarkdown(target, text) {
  target.textContent = "";
  const parts = String(text).split(/```(\w*)\n?([\s\S]*?)```/g);
  for (let i = 0; i < parts.length; i += 3) {
    if (parts[i]) {
      const p = el("div", "text-run");
      p.style.whiteSpace = "pre-wrap";
      p.textContent = parts[i].replace(/\n{3,}/g, "\n\n");
      target.appendChild(p);
    }
    if (i + 2 < parts.length) {
      const pre = el("pre");
      pre.textContent = parts[i + 2];
      target.appendChild(pre);
    }
  }
}

function addUser(text) {
  const m = el("div", "msg");
  const b = el("div", "user-msg", text);
  m.appendChild(b);
  chat.appendChild(m);
  autoscroll(true);
}

function addSys(text) {
  chat.appendChild(el("div", "sysline", text));
  autoscroll();
}

function thinkingBlock(open) {
  const d = document.createElement("details");
  d.className = "thinking" + (open ? " streaming" : "");
  if (open) d.open = true;
  const s = el("summary");
  s.appendChild(el("span", "chev", "\u25B6"));
  s.appendChild(el("span", "label", "Thinking"));
  d.appendChild(s);
  d.appendChild(el("div", "body", ""));
  return d;
}

function toolCard(name, inputText) {
  const d = document.createElement("details");
  d.className = "tool";
  const s = el("summary");
  s.appendChild(el("span", "chev", "\u25B6"));
  s.appendChild(el("span", "label", name));
  s.appendChild(el("span", "tool-status", "running…"));
  d.appendChild(s);
  const body = el("div", "body");
  const inp = el("div", "tool-in", inputText || "");
  body.appendChild(inp);
  d.appendChild(body);
  return d;
}

function summarizeToolInput(name, input) {
  try {
    if (!input) return "";
    if (typeof input === "string") return input.slice(0, 2000);
    if (input.command) return "$ " + input.command;
    if (input.file_path) return input.file_path + (input.old_string ? "  (edit)" : "");
    if (input.pattern) return "pattern: " + input.pattern;
    if (input.prompt) return String(input.prompt).slice(0, 400);
    return JSON.stringify(input, null, 1).slice(0, 2000);
  } catch { return ""; }
}

function ensureStream() {
  if (stream) return stream;
  const wrap = el("div", "msg assistant-msg");
  chat.appendChild(wrap);
  stream = { wrap, blocks: {} };
  return stream;
}

function handleStreamEvent(ev) {
  const s = ensureStream();
  if (ev.type === "content_block_start") {
    const cb = ev.content_block || {};
    if (cb.type === "thinking") {
      const d = thinkingBlock(true);
      s.blocks[ev.index] = { kind: "thinking", elem: d, buf: "" };
      s.wrap.appendChild(d);
      setBusy(true, "thinking…");
    } else if (cb.type === "text") {
      const t = el("div", "text-block");
      s.blocks[ev.index] = { kind: "text", elem: t, buf: "" };
      s.wrap.appendChild(t);
      setBusy(true, "writing…");
    } else if (cb.type === "tool_use") {
      const card = toolCard(cb.name || "tool", "");
      s.blocks[ev.index] = { kind: "tool", elem: card, buf: "", name: cb.name, id: cb.id };
      if (cb.id) toolCards[cb.id] = card;
      s.wrap.appendChild(card);
      setBusy(true, "using " + (cb.name || "tool") + "…");
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
      b.elem.querySelector(".tool-in").textContent = b.buf.slice(0, 2000);
    }
    autoscroll();
  } else if (ev.type === "content_block_stop") {
    const b = s.blocks[ev.index];
    if (b && b.kind === "thinking") {
      b.elem.classList.remove("streaming");
      b.elem.open = false;                  // collapse when done; click to expand
    }
    if (b && b.kind === "tool") {
      const pretty = summarizeToolInput(b.name, safeParse(b.buf));
      if (pretty) b.elem.querySelector(".tool-in").textContent = pretty;
    }
  }
}

function safeParse(s) { try { return JSON.parse(s); } catch { return s; } }

// complete assistant message: reconcile (covers non-streamed runs too)
function handleAssistant(msg) {
  const s = ensureStream();
  const streamedKinds = Object.keys(s.blocks).length > 0;
  for (const c of msg.content || []) {
    if (c.type === "thinking" && !streamedKinds) {
      const d = thinkingBlock(false);
      d.querySelector(".body").textContent = c.thinking || "";
      s.wrap.appendChild(d);
    } else if (c.type === "text" && !streamedKinds) {
      const t = el("div", "text-block");
      renderMarkdown(t, c.text || "");
      s.wrap.appendChild(t);
    } else if (c.type === "tool_use") {
      let card = c.id && toolCards[c.id];
      if (!card) {
        card = toolCard(c.name || "tool", "");
        if (c.id) toolCards[c.id] = card;
        s.wrap.appendChild(card);
      }
      const inEl = card.querySelector(".tool-in");
      const pretty = summarizeToolInput(c.name, c.input);
      if (pretty && (!inEl.textContent || inEl.textContent.length < pretty.length)) {
        inEl.textContent = pretty;
      }
    }
  }
  // message boundary: next content starts a new bubble
  stream = null;
  autoscroll();
}

function handleToolResult(c) {
  const card = toolCards[c.tool_use_id];
  if (!card) return;
  const status = card.querySelector(".tool-status");
  let out = "";
  if (typeof c.content === "string") out = c.content;
  else if (Array.isArray(c.content)) {
    out = c.content.map((x) => (x && x.type === "text" ? x.text : "")).join("\n");
  }
  const body = card.querySelector(".body");
  const outEl = el("div", "tool-out" + (c.is_error ? " err" : ""));
  outEl.textContent = (out || "(no output)").slice(0, 6000);
  body.appendChild(outEl);
  if (status) {
    status.textContent = c.is_error ? "error" : "done";
    if (c.is_error) status.classList.add("err");
  }
  autoscroll();
}

function fmtTokens(u) {
  if (!u) return "";
  const inTok = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
  return `${inTok.toLocaleString()} in / ${(u.output_tokens || 0).toLocaleString()} out`;
}

window.addEventListener("message", (e) => {
  const ev = e.data;
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init") {
        document.getElementById("model").textContent = ev.model || "local";
        addSys(`session ${String(ev.session_id || "").slice(0, 8)} · ${ev.model || ""} · ${ev.cwd || ""}`);
        setBusy(false);
      }
      break;
    case "stream_event":
      handleStreamEvent(ev.event || {});
      break;
    case "assistant":
      handleAssistant(ev.message || {});
      break;
    case "user": {
      for (const c of (ev.message && ev.message.content) || []) {
        if (c && c.type === "tool_result") handleToolResult(c);
      }
      break;
    }
    case "result": {
      const meta = el("div", "turn-meta",
        `turn done · ${Math.round((ev.duration_ms || 0) / 1000)}s · ${fmtTokens(ev.usage)}`);
      chat.appendChild(meta);
      stream = null;
      setBusy(false);
      autoscroll();
      break;
    }
    case "proc-exit":
      addSys(`engine exited (${ev.code}). Press Restart to spawn a fresh session.`);
      setBusy(false, "exited");
      statusDot.className = "dot";
      break;
    case "proc-error":
      addSys("engine error: " + ev.text);
      setBusy(false, "error");
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
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
});
setBusy(false, "starting engine…");
vscode.postMessage({ type: "ready" });
