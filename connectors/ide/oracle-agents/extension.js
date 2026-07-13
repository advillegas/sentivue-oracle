/* SentiVue Oracle Agents - the agent sidebar, living in the Secondary Side Bar.
 *
 * Top section is the live registry: every open agent and mission, click to jump
 * to its tab. Agents and missions run as editor-area terminal tabs, so any
 * number can be open side by side; the sidebar is the switchboard.
 * Zero dependencies; reads plain-text platform state straight off disk.
 */
"use strict";
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn } = require("child_process");

const WIN = process.platform === "win32";
let ROOT = "";
let EXT_URI = null;

function findRoot() {
  const folders = vscode.workspace.workspaceFolders || [];
  for (const f of folders) {
    if (fs.existsSync(path.join(f.uri.fsPath, "serving", "models.manifest"))) return f.uri.fsPath;
  }
  const home = path.join(os.homedir(), "sentivue-oracle");
  if (fs.existsSync(path.join(home, "serving", "models.manifest"))) return home;
  return folders.length ? folders[0].uri.fsPath : "";
}

function read(file) {
  try { return fs.readFileSync(file, "utf8"); } catch { return ""; }
}

// ---- live registry: agent/mission terminals -----------------------------------

const AGENT_RE = /^(Agent|Mission): /;
const started = new Map(); // terminal -> epoch ms (for uptime display)

function liveTerminals() {
  return vscode.window.terminals.filter((t) => AGENT_RE.test(t.name));
}

function uptime(t) {
  const s = started.get(t);
  if (!s) return "";
  const m = Math.floor((Date.now() - s) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function agentTerminal(kind) {
  // kind: claude | claude-wt | opencode | kilo. One independent engine session per call.
  const script = path.join(ROOT, "connectors", "ide", WIN ? "agent-tab.ps1" : "agent-tab.sh");
  const engine = kind === "opencode" ? "opencode" : kind === "kilo" ? "kilo" : "claude";
  const wt = kind === "claude-wt";
  const label = { claude: "Claude", opencode: "OpenCode", kilo: "Kilo" }[engine];
  const icon = { claude: "hubot", opencode: "rocket", kilo: "circuit-board" }[engine];
  const n = liveTerminals().filter((t) => t.name.startsWith("Agent:")).length + 1;
  const opts = {
    name: `Agent: ${label} ${n}${wt ? " (worktree)" : ""}`,
    location: vscode.TerminalLocation.Editor,   // real tabs: many at once, split, drag
    iconPath: new vscode.ThemeIcon(wt ? "git-branch" : icon),
  };
  if (WIN) {
    opts.shellPath = "powershell.exe";
    opts.shellArgs = ["-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, engine]
      .concat(wt ? ["-Worktree"] : []);
  } else {
    opts.shellPath = "/bin/bash";
    opts.shellArgs = [script, engine].concat(wt ? ["--worktree"] : []);
  }
  const t = vscode.window.createTerminal(opts);
  started.set(t, Date.now());
  t.show();
  return t;
}

function missionTerminal(tomlPath, engine, hours) {
  const name = `Mission: ${path.basename(tomlPath, ".toml")}`;
  const opts = {
    name,
    location: vscode.TerminalLocation.Editor,
    iconPath: new vscode.ThemeIcon("target"),
  };
  if (WIN) {
    opts.shellPath = "powershell.exe";
    opts.shellArgs = ["-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
      `& '${path.join(ROOT, "bin", "oracle.ps1")}' mission '${tomlPath}' ${engine} ${hours}`];
  } else {
    opts.shellPath = "/bin/bash";
    opts.shellArgs = ["-c", `'${path.join(ROOT, "bin", "oracle")}' mission '${tomlPath}' ${engine} ${hours}; exec bash -i`];
  }
  const t = vscode.window.createTerminal(opts);
  started.set(t, Date.now());
  t.show();
  return t;
}

// ---- conversation panels (Cursor-style chat over claude stream-json) ------------

const conversations = new Map(); // id -> ConversationPanel
let convSeq = 0;

function claudeBin() {
  const local = WIN
    ? path.join(ROOT, ".tools", "npm", "claude.cmd")
    : path.join(ROOT, ".tools", "npm", "bin", "claude");
  return fs.existsSync(local) ? local : "claude";
}

class ConversationPanel {
  constructor(refreshLive) {
    this.id = ++convSeq;
    this.name = `Chat: Claude ${this.id}`;
    this.refreshLive = refreshLive;
    this.child = null;
    this.buf = "";
    this.panel = vscode.window.createWebviewPanel(
      "oracleAgentChat", this.name, vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(EXT_URI, "media")],
      });
    this.panel.iconPath = vscode.Uri.joinPath(EXT_URI, "media", "oracle.svg");
    this.panel.webview.html = this.html();
    this.panel.onDidDispose(() => {
      this.kill();
      conversations.delete(this.id);
      this.refreshLive();
    });
    this.panel.webview.onDidReceiveMessage((m) => this.onMessage(m));
    conversations.set(this.id, this);
    this.refreshLive();
  }

  html() {
    const w = this.panel.webview;
    const css = w.asWebviewUri(vscode.Uri.joinPath(EXT_URI, "media", "chat.css"));
    const js = w.asWebviewUri(vscode.Uri.joinPath(EXT_URI, "media", "chat.js"));
    const nonce = String(Math.random()).slice(2);
    return `<!DOCTYPE html><html><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
  content="default-src 'none'; style-src ${w.cspSource}; script-src 'nonce-${nonce}'; img-src ${w.cspSource};">
<link rel="stylesheet" href="${css}">
</head><body>
<div id="header">
  <span class="title">Agent</span>
  <span id="model"></span>
  <span id="dot"></span>
  <span id="status">starting…</span>
  <span style="margin-left:auto"></span>
  <button id="stop" class="hbtn" disabled>Stop</button>
  <button id="restart" class="hbtn">Restart</button>
</div>
<div id="chat"><div id="col"></div></div>
<div id="composer-wrap"><div id="composer">
  <div id="input-box">
    <textarea id="input" rows="1" placeholder="Plan, build, run - the agent has full tool access here"></textarea>
    <button id="send" title="Send (Enter)">&#8593;</button>
  </div>
</div></div>
<script nonce="${nonce}" src="${js}"></script>
</body></html>`;
  }

  spawnEngine() {
    this.kill();
    const env = { ...process.env };
    env.CLAUDE_CONFIG_DIR = path.join(ROOT, "engines", "claude-code", "home");
    env.ORACLE_ROOT = ROOT;
    env.ORACLE_PROJECT_ROOT = ROOT;
    env.LEAN_CTX_CONFIG_DIR = path.join(ROOT, "state", "lean-ctx", "config");
    env.LEAN_CTX_DATA_DIR = path.join(ROOT, "state", "lean-ctx", "data");
    env.LEAN_CTX_STATE_DIR = path.join(ROOT, "state", "lean-ctx", "state");
    env.LEAN_CTX_CACHE_DIR = path.join(ROOT, "state", "lean-ctx", "cache");
    env.LEAN_CTX_PROJECT_ROOT = env.ORACLE_PROJECT_ROOT;
    env.LEAN_CTX_TOOL_PROFILE = "minimal";
    env.LEAN_CTX_DISABLED_TOOLS = "ctx_call";
    env.LEAN_CTX_NO_UPDATE_CHECK = "1";
    env.LEAN_CTX_AUTONOMY = "false";
    env.LEAN_CTX_NO_HOOK = "1";
    env.LEAN_CTX_RULES_INJECTION = "off";
    env.PATH = [path.join(ROOT, "env", ".venv", WIN ? "Scripts" : "bin"),
                path.join(ROOT, ".tools", "bin"),
                path.join(ROOT, ".tools", "npm"),
                path.join(ROOT, ".tools", "npm", "bin"),
                env.PATH || ""].join(path.delimiter);
    const args = [
      "--input-format", "stream-json",
      "--output-format", "stream-json",
      "--include-partial-messages",
      "--verbose",
      "--dangerously-skip-permissions",
      "--mcp-config", path.join(ROOT, "connectors", "mcp.claude.json"),
    ];
    try {
      this.child = spawn(claudeBin(), args, { cwd: ROOT, env, shell: WIN });
    } catch (e) {
      this.post({ type: "proc-error", text: String(e) });
      return;
    }
    this.child.stdout.on("data", (d) => this.onData(String(d)));
    this.child.stderr.on("data", (d) => {
      const t = String(d).trim();
      if (t) this.post({ type: "proc-error", text: t.slice(0, 400) });
    });
    this.child.on("exit", (code) => this.post({ type: "proc-exit", code }));
    this.child.on("error", (e) => this.post({ type: "proc-error", text: String(e) }));
  }

  onData(chunk) {
    this.buf += chunk;
    let nl;
    while ((nl = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, nl).trim();
      this.buf = this.buf.slice(nl + 1);
      if (!line.startsWith("{")) continue;
      try { this.post(JSON.parse(line)); } catch { /* partial/garbage line */ }
    }
  }

  post(ev) {
    try { this.panel.webview.postMessage(ev); } catch { /* disposed */ }
  }

  onMessage(m) {
    if (m.type === "ready") {
      this.spawnEngine();
    } else if (m.type === "send") {
      if (!this.child || this.child.exitCode !== null) this.spawnEngine();
      this.post({ type: "echo-user", text: m.text });
      const msg = { type: "user",
        message: { role: "user", content: [{ type: "text", text: m.text }] } };
      try { this.child.stdin.write(JSON.stringify(msg) + "\n"); }
      catch (e) { this.post({ type: "proc-error", text: "stdin write failed: " + e }); }
    } else if (m.type === "stop") {
      // best effort: control-protocol interrupt, then a hard kill fallback
      try {
        this.child.stdin.write(JSON.stringify(
          { type: "control_request", request_id: `int-${Date.now()}`,
            request: { subtype: "interrupt" } }) + "\n");
      } catch { this.kill(); }
    } else if (m.type === "restart") {
      this.spawnEngine();
    }
  }

  kill() {
    if (!this.child) return;
    try {
      if (WIN) spawn("taskkill", ["/F", "/T", "/PID", String(this.child.pid)]);
      else this.child.kill("SIGKILL");
    } catch { /* already gone */ }
    this.child = null;
  }

  reveal() { this.panel.reveal(); }
}

// ---- mission state parsing ------------------------------------------------------

function missionState() {
  const txt = read(path.join(ROOT, "memory", "STATE.md"));
  if (!txt) return null;
  const head = txt.match(/^# STATE .*?`([^`]+)`/m);
  const now = txt.match(/^Now: (.+)$/m);
  const left = txt.match(/Time left: ([\d.]+ h)/);
  const tasks = [];
  for (const m of txt.matchAll(/^\| (\S+) \| (\S+) \| (\S+) \| (\S+) \| (.+?) \|$/gm)) {
    if (m[1] === "task" || m[1].startsWith("-")) continue;
    tasks.push({ id: m[1], status: m[2], attempts: m[3], tier: m[4], title: m[5] });
  }
  return { name: head ? head[1] : "?", now: now ? now[1] : "", left: left ? left[1] : "", tasks };
}

const STATUS_ICON = {
  done: "pass-filled", running: "sync~spin", auditing: "shield", claimed: "arrow-right",
  pending: "circle-large-outline", failed: "error", blocked: "circle-slash", "merge-conflict": "git-merge",
};

// ---- tree plumbing ---------------------------------------------------------------

function item(label, opts = {}) {
  const it = new vscode.TreeItem(label, opts.children && opts.children.length
    ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
  if (opts.icon) it.iconPath = new vscode.ThemeIcon(opts.icon);
  if (opts.desc) it.description = opts.desc;
  if (opts.tip) it.tooltip = opts.tip;
  if (opts.ctx) it.contextValue = opts.ctx;
  if (opts.cmd) it.command = { command: opts.cmd, title: label, arguments: opts.args || [] };
  it._children = opts.children || null;
  return it;
}

class Tree {
  constructor(build) { this.build = build; this._e = new vscode.EventEmitter(); this.onDidChangeTreeData = this._e.event; }
  refresh() { this._e.fire(); }
  getTreeItem(e) { return e; }
  getChildren(e) { return e ? (e._children || []) : this.build(); }
}

// ---- view builders ----------------------------------------------------------------

function buildLive() {
  const out = [];
  const live = liveTerminals();
  const missions = live.filter((t) => t.name.startsWith("Mission:"));
  const agents = live.filter((t) => t.name.startsWith("Agent:"));
  const chats = [...conversations.values()];
  if (missions.length) {
    out.push(item("RUNNING MISSIONS", {
      icon: "target",
      children: missions.map((t, i) => item(t.name.replace(/^Mission: /, ""), {
        icon: "sync~spin", desc: uptime(t), ctx: "runningAgent",
        tip: "Click to open this mission's tab",
        cmd: "oracleAgents.reveal", args: [i + ":mission"],
      })),
    }));
  }
  if (chats.length || agents.length) {
    out.push(item("OPEN AGENTS", {
      icon: "hubot",
      children: [
        ...chats.map((c) => item(c.name.replace(/^Chat: /, ""), {
          icon: "comment-discussion", desc: "chat", ctx: "runningAgent",
          tip: "Click to open this conversation",
          cmd: "oracleAgents.revealChat", args: [c.id],
        })),
        ...agents.map((t, i) => item(t.name.replace(/^Agent: /, ""), {
          icon: t.name.includes("worktree") ? "git-branch" : t.name.includes("OpenCode") ? "rocket" : "terminal",
          desc: uptime(t), ctx: "runningAgent",
          tip: "Click to open this agent's tab",
          cmd: "oracleAgents.reveal", args: [i + ":agent"],
        })),
      ],
    }));
  }
  // When nothing is running, return [] so the view's welcome content (action
  // buttons) renders instead. The launchers used to be static tree rows here,
  // but that made this top view tall enough to push Mission Status / Session
  // Journals off-screen; they now live in the title-bar "+" menu and welcome
  // view, keeping this section compact so the other views stay visible.
  return out;
}

function buildMission() {
  const st = missionState();
  if (!st) return [item("No mission has run yet", { icon: "info" })];
  const tasks = st.tasks.map((t) => item(t.id, {
    icon: STATUS_ICON[t.status] || "question", desc: `${t.status} (${t.attempts}, ${t.tier})`, tip: t.title,
  }));
  return [
    item(st.name, { icon: "target", desc: st.left ? `${st.left} left` : "", tip: st.now, children: tasks }),
    item(("Now: " + (st.now || "idle")).slice(0, 70), { icon: "pulse", tip: st.now }),
    item("Latest report", { icon: "notebook", cmd: "oracleAgents.openLatestReport" }),
    item("Ledger", { icon: "history", cmd: "oracleAgents.openLedger" }),
  ];
}

function buildSessions() {
  const dir = path.join(ROOT, "memory", "sessions");
  let files = [];
  try {
    files = fs.readdirSync(dir).filter((f) => f.endsWith(".md"))
      .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtime }))
      .sort((a, b) => b.t - a.t).slice(0, 15);
  } catch { /* none yet */ }
  if (!files.length) return [item("No session journals yet", { icon: "info", tip: "Every agent session writes memory/sessions/<id>.md" })];
  return files.map(({ f, t }) => {
    const doing = /## DOING[^\n]*\n- (.+)/.exec(read(path.join(dir, f)));
    return item(f.replace(/\.md$/, "").slice(0, 26), {
      icon: "comment-discussion",
      desc: t.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
      tip: doing ? `DOING: ${doing[1]}` : f,
      cmd: "vscode.open", args: [vscode.Uri.file(path.join(dir, f))],
    });
  });
}

// ---- activation --------------------------------------------------------------------

function activate(ctx) {
  ROOT = findRoot();
  EXT_URI = ctx.extensionUri;
  const trees = { live: new Tree(buildLive), mission: new Tree(buildMission), sessions: new Tree(buildSessions) };
  const refreshAll = () => Object.values(trees).forEach((t) => t.refresh());

  // Adopt agent terminals that already existed before activation (e.g. reload).
  for (const t of liveTerminals()) if (!started.has(t)) started.set(t, Date.now());

  // One-time cleanup of v0.1: it moved the whole terminal panel into the
  // secondary side bar; this container now lives there natively, so put the
  // panel back where it belongs.
  if (!ctx.globalState.get("oracle.v2.panelRestored")) {
    ctx.globalState.update("oracle.v2.panelRestored", true);
    if (ctx.globalState.get("oracle.dockedOnce")) {
      vscode.commands.executeCommand("workbench.action.positionPanelBottom");
    }
  }

  ctx.subscriptions.push(
    vscode.window.registerTreeDataProvider("oracleAgents.live", trees.live),
    vscode.window.registerTreeDataProvider("oracleAgents.mission", trees.mission),
    vscode.window.registerTreeDataProvider("oracleAgents.sessions", trees.sessions),
    vscode.window.onDidOpenTerminal((t) => { if (AGENT_RE.test(t.name) && !started.has(t)) started.set(t, Date.now()); trees.live.refresh(); }),
    vscode.window.onDidCloseTerminal((t) => { started.delete(t); trees.live.refresh(); }),

    vscode.commands.registerCommand("oracleAgents.orchestrationViewer", () => {
      const setup = WIN
        ? { shellPath: "powershell.exe",
            shellArgs: ["-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
              `& '${path.join(ROOT, "harness", "agent-mcp", "setup-agent-mcp.ps1")}' install; ` +
              `& '${path.join(ROOT, "harness", "agent-mcp", "setup-agent-mcp.ps1")}' start`] }
        : { shellPath: "/bin/bash",
            shellArgs: ["-c",
              `bash '${path.join(ROOT, "harness", "agent-mcp", "setup-agent-mcp.sh")}' install && ` +
              `bash '${path.join(ROOT, "harness", "agent-mcp", "setup-agent-mcp.sh")}' start; exec bash -i`] };
      const t = vscode.window.createTerminal({
        name: "Agent-MCP Viewer", iconPath: new vscode.ThemeIcon("type-hierarchy"), ...setup,
      });
      t.show();
      setTimeout(() => vscode.env.openExternal(vscode.Uri.parse("http://127.0.0.1:3847")), 25000);
    }),
    vscode.commands.registerCommand("oracleAgents.newConversation", () =>
      new ConversationPanel(() => trees.live.refresh())),
    vscode.commands.registerCommand("oracleAgents.revealChat", (id) => {
      const c = conversations.get(Number(id));
      if (c) c.reveal();
    }),
    vscode.commands.registerCommand("oracleAgents.newClaude", () => agentTerminal("claude")),
    vscode.commands.registerCommand("oracleAgents.newClaudeWorktree", () => agentTerminal("claude-wt")),
    vscode.commands.registerCommand("oracleAgents.newOpenCode", () => agentTerminal("opencode")),
    vscode.commands.registerCommand("oracleAgents.newKilo", () => agentTerminal("kilo")),
    vscode.commands.registerCommand("oracleAgents.refresh", refreshAll),
    vscode.commands.registerCommand("oracleAgents.reveal", (key) => {
      const [idx, kind] = String(key).split(":");
      const pool = liveTerminals().filter((t) => t.name.startsWith(kind === "mission" ? "Mission:" : "Agent:"));
      const t = pool[Number(idx)];
      if (t) t.show();
    }),
    vscode.commands.registerCommand("oracleAgents.kill", (node) => {
      const label = node && node.label;
      const t = liveTerminals().find((x) => x.name.endsWith(String(label)));
      if (t) t.dispose();
    }),
    vscode.commands.registerCommand("oracleAgents.openLedger", () =>
      vscode.commands.executeCommand("vscode.open", vscode.Uri.file(path.join(ROOT, "memory", "LEDGER.md")))),
    vscode.commands.registerCommand("oracleAgents.openLatestReport", () => {
      const dir = path.join(ROOT, "reports");
      let latest = null;
      try {
        latest = fs.readdirSync(dir).filter((f) => f.endsWith(".md"))
          .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
          .sort((a, b) => b.t - a.t)[0];
      } catch { /* none */ }
      if (!latest) return vscode.window.showInformationMessage("No mission reports yet.");
      vscode.commands.executeCommand("vscode.open", vscode.Uri.file(path.join(dir, latest.f)));
    }),
    vscode.commands.registerCommand("oracleAgents.startMission", async () => {
      const dir = path.join(ROOT, "conductor", "missions");
      let missions = [];
      try { missions = fs.readdirSync(dir).filter((f) => f.endsWith(".toml")); } catch { /* none */ }
      if (!missions.length) return vscode.window.showWarningMessage("No mission files under conductor/missions.");
      const pick = await vscode.window.showQuickPick(missions, { placeHolder: "Mission to run (self-governing loop)" });
      if (!pick) return;
      const engine = await vscode.window.showQuickPick(["claude", "opencode", "kilo"], { placeHolder: "Engine" });
      if (!engine) return;
      const hours = await vscode.window.showInputBox({ prompt: "Hour budget", value: "24" });
      if (!hours) return;
      missionTerminal(path.join(dir, pick), engine, hours);
    }),
  );

  // Live refresh from disk: mission state, reports, session journals.
  for (const rel of ["memory/STATE.md", "memory/sessions/*.md", "reports/*.md"]) {
    const w = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(ROOT, rel));
    w.onDidChange(refreshAll); w.onDidCreate(refreshAll); w.onDidDelete(refreshAll);
    ctx.subscriptions.push(w);
  }
  // Uptime ticker.
  const tick = setInterval(() => trees.live.refresh(), 60000);
  ctx.subscriptions.push({ dispose: () => clearInterval(tick) });
}

function deactivate() {}
module.exports = { activate, deactivate };
