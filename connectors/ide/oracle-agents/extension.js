/* SentiVue Oracle Agents - the agents sidebar.
 *
 * Three tree views (Agents / Mission / Sessions) plus commands that launch
 * parallel agent terminals, start conductor missions, and open the memory
 * files. Zero dependencies; reads plain-text state straight off disk.
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

function agentTerminal(kind) {
  // kind: claude | claude-wt | opencode. One independent engine session per call.
  const script = path.join(ROOT, "connectors", "ide", WIN ? "agent-tab.ps1" : "agent-tab.sh");
  const engine = kind === "opencode" ? "opencode" : "claude";
  const wt = kind === "claude-wt";
  const name = `Agent: ${engine === "claude" ? "Claude" : "OpenCode"}${wt ? " (worktree)" : ""}`;
  // TerminalLocation.Panel = wherever the terminal panel lives; after the
  // first-run auto-dock that is the secondary side bar (Cursor-style).
  const opts = {
    name,
    location: vscode.TerminalLocation.Panel,
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
  t.show();
}

// ---- mission state parsing ---------------------------------------------------

function missionState() {
  const txt = read(path.join(ROOT, "memory", "STATE.md"));
  if (!txt) return null;
  const head = /^# STATE — mission `([^`]+)`/m.exec(txt) || /^# STATE - mission `([^`]+)`/m.exec(txt);
  const now = /^Now: (.+)$/m.exec(txt);
  const left = /Time left: ([\d.]+ h)/.exec(txt);
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

// ---- tree items ----------------------------------------------------------------

function item(label, opts = {}) {
  const it = new vscode.TreeItem(label, opts.children
    ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
  if (opts.icon) it.iconPath = new vscode.ThemeIcon(opts.icon);
  if (opts.desc) it.description = opts.desc;
  if (opts.tip) it.tooltip = opts.tip;
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

function buildAgents() {
  return [
    item("New Agent (Claude Code)", { icon: "add", cmd: "oracleAgents.newClaude", tip: "Full engine session as an editor tab - open as many as you want" }),
    item("New Agent in Worktree", { icon: "git-branch", cmd: "oracleAgents.newClaudeWorktree", tip: "Isolated git worktree + branch: parallel agents never collide" }),
    item("New Agent (OpenCode)", { icon: "rocket", cmd: "oracleAgents.newOpenCode" }),
    item("Dock agents right (secondary side bar)", { icon: "layout-sidebar-right", cmd: "oracleAgents.dockRight", tip: "Move the terminal panel into the secondary side bar, Cursor-style" }),
  ];
}

function buildMission() {
  const st = missionState();
  if (!st) return [item("No mission state yet", { icon: "info", desc: "start one below" }),
                   item("Start Mission...", { icon: "play", cmd: "oracleAgents.startMission" })];
  const tasks = st.tasks.map((t) => item(t.id, {
    icon: STATUS_ICON[t.status] || "question", desc: `${t.status} (${t.attempts}, ${t.tier})`, tip: t.title,
  }));
  return [
    item(st.name, { icon: "target", desc: st.left ? `${st.left} left` : "", tip: st.now, children: tasks }),
    item("Now: " + (st.now || "idle").slice(0, 60), { icon: "pulse", tip: st.now }),
    item("Start Mission...", { icon: "play", cmd: "oracleAgents.startMission" }),
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
    return item(f.replace(/\.md$/, "").slice(0, 24), {
      icon: "comment-discussion",
      desc: t.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
      tip: doing ? `DOING: ${doing[1]}` : f,
      cmd: "vscode.open", args: [vscode.Uri.file(path.join(dir, f))],
    });
  });
}

// ---- activation ------------------------------------------------------------------

function activate(ctx) {
  ROOT = findRoot();
  // First activation: dock the terminal panel into the secondary side bar so
  // agent tabs live on the right, Cursor-style. Once only - the user's later
  // layout choices (or "Restore Terminal Panel to Bottom") are respected.
  if (!ctx.globalState.get("oracle.dockedOnce")) {
    ctx.globalState.update("oracle.dockedOnce", true);
    vscode.commands.executeCommand("workbench.action.movePanelToSecondarySideBar");
  }
  const trees = {
    agents: new Tree(buildAgents),
    mission: new Tree(buildMission),
    sessions: new Tree(buildSessions),
  };
  ctx.subscriptions.push(
    vscode.window.registerTreeDataProvider("oracleAgents.agents", trees.agents),
    vscode.window.registerTreeDataProvider("oracleAgents.mission", trees.mission),
    vscode.window.registerTreeDataProvider("oracleAgents.sessions", trees.sessions),
    vscode.commands.registerCommand("oracleAgents.newClaude", () => agentTerminal("claude")),
    vscode.commands.registerCommand("oracleAgents.newClaudeWorktree", () => agentTerminal("claude-wt")),
    vscode.commands.registerCommand("oracleAgents.newOpenCode", () => agentTerminal("opencode")),
    vscode.commands.registerCommand("oracleAgents.refresh", () => Object.values(trees).forEach((t) => t.refresh())),
    vscode.commands.registerCommand("oracleAgents.dockRight", () =>
      vscode.commands.executeCommand("workbench.action.movePanelToSecondarySideBar")),
    vscode.commands.registerCommand("oracleAgents.dockBottom", () =>
      vscode.commands.executeCommand("workbench.action.positionPanelBottom")),
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
      const pick = await vscode.window.showQuickPick(missions, { placeHolder: "Mission to run (conductor loop)" });
      if (!pick) return;
      const engine = await vscode.window.showQuickPick(["claude", "opencode"], { placeHolder: "Engine" });
      if (!engine) return;
      const hours = await vscode.window.showInputBox({ prompt: "Hour budget", value: "24" });
      if (!hours) return;
      const t = vscode.window.createTerminal({
        name: `Mission: ${pick.replace(/\.toml$/, "")}`,
        location: vscode.TerminalLocation.Editor,
        iconPath: new vscode.ThemeIcon("target"),
        shellPath: WIN ? "powershell.exe" : "/bin/bash",
        shellArgs: WIN
          ? ["-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             `& '${path.join(ROOT, "bin", "oracle.ps1")}' mission '${path.join(dir, pick)}' ${engine} ${hours}`]
          : ["-c", `'${path.join(ROOT, "bin", "oracle")}' mission '${path.join(dir, pick)}' ${engine} ${hours}; exec bash -i`],
      });
      t.show();
    }),
  );

  // Live refresh: mission state, reports, and session journals change on disk.
  for (const rel of ["memory/STATE.md", "memory/sessions/*.md", "reports/*.md"]) {
    const w = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(ROOT, rel));
    const kick = () => { trees.mission.refresh(); trees.sessions.refresh(); };
    w.onDidChange(kick); w.onDidCreate(kick); w.onDidDelete(kick);
    ctx.subscriptions.push(w);
  }
}

function deactivate() {}
module.exports = { activate, deactivate };
