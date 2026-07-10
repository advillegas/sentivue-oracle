//! Engine bridge: drives Claude Code / OpenCode headlessly and streams events
//! back to the UI. Claude speaks stream-json (structured events, tool calls,
//! session ids for multi-turn); OpenCode streams plain text with --continue.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum EngineKind {
    Claude,
    OpenCode,
}

impl EngineKind {
    pub fn label(&self) -> &'static str {
        match self {
            EngineKind::Claude => "Claude Code",
            EngineKind::OpenCode => "OpenCode",
        }
    }
}

pub enum Event {
    Text(String),
    Tool(String),
    Done { session_id: Option<String> },
    Fail(String),
}

pub struct RunHandle {
    pub rx: Receiver<Event>,
    pub child: Arc<Mutex<Option<Child>>>,
}

impl RunHandle {
    pub fn stop(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(c) = guard.as_mut() {
                let _ = c.kill();
            }
        }
    }
}

/// Spawn one headless engine turn. Returns immediately; events stream on rx.
pub fn send(root: PathBuf, kind: EngineKind, session_id: Option<String>, prompt: String) -> RunHandle {
    let (tx, rx) = channel::<Event>();
    let child_slot: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let slot = child_slot.clone();

    thread::spawn(move || {
        let mut cmd = Command::new("bash");
        match kind {
            EngineKind::Claude => {
                cmd.arg(root.join("engines/claude-code/launch.sh"))
                    .arg("-p")
                    .arg(&prompt)
                    .args(["--output-format", "stream-json", "--verbose",
                           "--dangerously-skip-permissions"]);
                if let Some(sid) = &session_id {
                    cmd.args(["--resume", sid]);
                }
            }
            EngineKind::OpenCode => {
                cmd.arg(root.join("engines/opencode/launch.sh")).arg("run");
                if session_id.is_some() {
                    cmd.arg("--continue");
                }
                cmd.arg(&prompt);
            }
        }
        cmd.current_dir(&root)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .stdin(Stdio::null());

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(Event::Fail(format!(
                    "could not start engine ({e}). On the Mac appliance this needs \
                     bash + the installed engines; chat is unavailable on this node."
                )));
                return;
            }
        };
        let stdout = child.stdout.take();
        if let Ok(mut guard) = slot.lock() {
            *guard = Some(child);
        }
        let Some(stdout) = stdout else {
            let _ = tx.send(Event::Fail("no stdout from engine".into()));
            return;
        };

        let mut found_session: Option<String> = None;
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            match kind {
                EngineKind::Claude => parse_claude_line(&line, &tx, &mut found_session),
                EngineKind::OpenCode => {
                    let _ = tx.send(Event::Text(format!("{line}\n")));
                }
            }
        }
        if kind == EngineKind::OpenCode {
            // OpenCode --continue keys off its own last-session state.
            found_session = Some("continue".to_string());
        }
        let _ = tx.send(Event::Done { session_id: found_session });
    });

    RunHandle { rx, child: child_slot }
}

fn parse_claude_line(line: &str, tx: &Sender<Event>, session: &mut Option<String>) {
    let trimmed = line.trim();
    if !trimmed.starts_with('{') {
        return;
    }
    let Ok(v) = serde_json::from_str::<serde_json::Value>(trimmed) else { return };
    match v.get("type").and_then(|t| t.as_str()) {
        Some("assistant") => {
            if let Some(items) = v.pointer("/message/content").and_then(|c| c.as_array()) {
                for item in items {
                    match item.get("type").and_then(|t| t.as_str()) {
                        Some("text") => {
                            if let Some(t) = item.get("text").and_then(|t| t.as_str()) {
                                let _ = tx.send(Event::Text(t.to_string()));
                            }
                        }
                        Some("tool_use") => {
                            let name = item.get("name").and_then(|n| n.as_str()).unwrap_or("tool");
                            let _ = tx.send(Event::Tool(name.to_string()));
                        }
                        _ => {}
                    }
                }
            }
        }
        Some("result") => {
            if let Some(sid) = v.get("session_id").and_then(|s| s.as_str()) {
                *session = Some(sid.to_string());
            }
        }
        _ => {}
    }
}
