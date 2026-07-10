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

const WIN = process.platform === "win32";
let ROOT = "";

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
  // kind: claude | claude-wt | opencode. One independent engine session per call.
  const script = path.join(ROOT, "connectors", "ide", WIN ? "agent-tab.ps1" : "agent-tab.sh");
  const engine = kind === "opencode" ? "opencode" : "claude";
  const wt = kind === "claude-wt";
  const n = liveTerminals().filter((t) => t.name.startsWith("Agent:")).length + 1;
  const opts = {
    name: `Agent: ${engine === "claude" ? "Claude" : "OpenCode"} ${n}${wt ? " (worktree)" : ""}`,
    location: vscode.TerminalLocation.Editor,   // real tabs: many at once, split, drag
    iconPath: new vscode.ThemeIcon(wt ? "git-branch" : engine === "claude" ? "hubot" : "rocket"),
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
  if (agents.length) {
    out.push(item("OPEN AGENTS", {
      icon: "hubot",
      children: agents.map((t, i) => item(t.name.replace(/^Agent: /, ""), {
        icon: t.name.includes("worktree") ? "git-branch" : t.name.includes("OpenCode") ? "rocket" : "hubot",
        desc: uptime(t), ctx: "runningAgent",
        tip: "Click to open this agent's tab",
        cmd: "oracleAgents.reveal", args: [i + ":agent"],
      })),
    }));
  }
  if (!live.length) out.push(item("Nothing running", { icon: "info", desc: "open an agent or mission below" }));
  out.push(item("New Agent (Claude Code)", { icon: "add", cmd: "oracleAgents.newClaude", tip: "Full engine session as an editor tab" }));
  out.push(item("New Agent in Worktree", { icon: "git-branch", cmd: "oracleAgents.newClaudeWorktree", tip: "Isolated worktree + branch: parallel agents never collide" }));
  out.push(item("New Agent (OpenCode)", { icon: "rocket", cmd: "oracleAgents.newOpenCode" }));
  out.push(item("Start Mission (autonomous loop)...", { icon: "play", cmd: "oracleAgents.startMission" }));
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

    vscode.commands.registerCommand("oracleAgents.newClaude", () => agentTerminal("claude")),
    vscode.commands.registerCommand("oracleAgents.newClaudeWorktree", () => agentTerminal("claude-wt")),
    vscode.commands.registerCommand("oracleAgents.newOpenCode", () => agentTerminal("opencode")),
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
      const engine = await vscode.window.showQuickPick(["claude", "opencode"], { placeHolder: "Engine" });
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
