//! Data layer: the mission-control panels read the ecosystem's plain-text
//! state directly from disk (the files ARE the API), llama-swap over
//! localhost HTTP, and the vault via git. No servers required.

use std::path::{Path, PathBuf};
use std::time::Duration;

pub fn find_root() -> Option<PathBuf> {
    if let Ok(r) = std::env::var("ORACLE_ROOT") {
        let p = PathBuf::from(r);
        if p.join("serving/models.manifest").exists() {
            return Some(p);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        for anc in exe.ancestors() {
            if anc.join("serving/models.manifest").exists() {
                return Some(anc.to_path_buf());
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        for anc in cwd.ancestors() {
            if anc.join("serving/models.manifest").exists() {
                return Some(anc.to_path_buf());
            }
        }
    }
    None
}

/// (fast, smart) chat models for this machine's profile, from serving/tiers.env.
pub fn tier_models(root: &Path) -> (String, String) {
    let mut fast = "qwen3-coder-30b".to_string();
    let mut smart = "qwen3-coder-480b".to_string();
    if let Ok(body) = std::fs::read_to_string(root.join("serving/tiers.env")) {
        for line in body.lines() {
            if let Some(v) = line.strip_prefix("HAIKU_MODEL=") {
                fast = v.trim().to_string();
            }
            if let Some(v) = line.strip_prefix("SONNET_MODEL=") {
                smart = v.trim().to_string();
            }
        }
    }
    (fast, smart)
}

pub fn default_install_dir() -> PathBuf {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".into());
    PathBuf::from(home).join("sentivue-oracle")
}

pub fn read_tail(path: &Path, lines: usize) -> String {
    match std::fs::read_to_string(path) {
        Ok(s) => {
            let all: Vec<&str> = s.lines().collect();
            let start = all.len().saturating_sub(lines);
            all[start..].join("\n")
        }
        Err(_) => String::from("(none yet)"),
    }
}

/// Task ids waiting for the operator countersign: mentioned as APPROVAL NEEDED
/// in the ledger and not yet APPROVE'd.
pub fn awaiting_approvals(root: &Path) -> Vec<String> {
    let ledger = std::fs::read_to_string(root.join("memory/LEDGER.md")).unwrap_or_default();
    let approved = std::fs::read_to_string(root.join("memory/APPROVALS.md")).unwrap_or_default();
    let approved_ids: Vec<String> = approved
        .lines()
        .filter_map(|l| l.trim().strip_prefix("APPROVE ").map(|s| s.trim().to_string()))
        .collect();
    let mut out: Vec<String> = Vec::new();
    for line in ledger.lines() {
        if let Some(idx) = line.find("APPROVAL NEEDED ") {
            let rest = &line[idx + "APPROVAL NEEDED ".len()..];
            let id: String = rest
                .chars()
                .take_while(|c| c.is_alphanumeric() || *c == '-' || *c == '_')
                .collect();
            if !id.is_empty() && !approved_ids.contains(&id) && !out.contains(&id) {
                out.push(id);
            }
        }
    }
    out
}

pub fn approve(root: &Path, id: &str) {
    let path = root.join("memory/APPROVALS.md");
    let mut body = std::fs::read_to_string(&path).unwrap_or_default();
    if !body.ends_with('\n') && !body.is_empty() {
        body.push('\n');
    }
    body.push_str(&format!("APPROVE {id}\n"));
    let _ = std::fs::create_dir_all(root.join("memory"));
    let _ = std::fs::write(&path, body);
}

pub fn list_reports(root: &Path) -> Vec<PathBuf> {
    let mut v: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(root.join("reports")) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().map(|x| x == "md").unwrap_or(false) {
                let t = e.metadata().and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH);
                v.push((t, p));
            }
        }
    }
    v.sort_by(|a, b| b.0.cmp(&a.0));
    v.into_iter().take(15).map(|(_, p)| p).collect()
}

pub struct ModelStatus {
    pub healthy: bool,
    pub running: String,
}

pub fn model_status() -> ModelStatus {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    let healthy = agent.get("http://127.0.0.1:9099/health").call().is_ok();
    let running = match agent.get("http://127.0.0.1:9099/running").call() {
        Ok(resp) => match resp.into_json::<serde_json::Value>() {
            Ok(v) => {
                let mut s = String::new();
                if let Some(arr) = v.get("running").and_then(|r| r.as_array()) {
                    for m in arr {
                        let name = m.get("model").and_then(|x| x.as_str()).unwrap_or("?");
                        let state = m.get("state").and_then(|x| x.as_str()).unwrap_or("?");
                        s.push_str(&format!("{name}  [{state}]\n"));
                    }
                }
                if s.is_empty() {
                    "(no models loaded yet - they load on first request)".into()
                } else {
                    s
                }
            }
            Err(_) => "(unparseable /running payload)".into(),
        },
        Err(_) => "(llama-swap not reachable)".into(),
    };
    ModelStatus { healthy, running }
}

pub fn vault_inventory(root: &Path) -> String {
    let vault = std::env::var("ORACLE_VAULT").ok().map(PathBuf::from).unwrap_or_else(|| {
        let home = std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_default();
        PathBuf::from(home).join("oracle-git-vault")
    });
    let Ok(rd) = std::fs::read_dir(&vault) else {
        return format!("no vault at {} (oracle vault init)", vault.display());
    };
    let mut out = format!("vault: {}\n\n", vault.display());
    for e in rd.flatten() {
        let p = e.path();
        if !p.is_dir() || p.extension().map(|x| x != "git").unwrap_or(true) {
            continue;
        }
        let name = p.file_stem().and_then(|s| s.to_str()).unwrap_or("?").to_string();
        let last = std::process::Command::new("git")
            .args(["-C"])
            .arg(&p)
            .args(["for-each-ref", "--count=1", "--sort=-committerdate",
                   "--format=%(committerdate:short) %(refname:short)", "refs/heads"])
            .output()
            .ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();
        let last = if last.is_empty() { "empty".to_string() } else { last };
        out.push_str(&format!("{name:<26} last: {last}\n"));
    }
    // Currency of this repo vs the vault, when the remote exists.
    let behind = std::process::Command::new("git")
        .args(["-C"])
        .arg(root)
        .args(["rev-list", "--count", "vault/main..main"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string());
    if let Some(b) = behind {
        out.push_str(&format!("\nthis repo: {} commit(s) not yet in the vault\n",
                              if b.is_empty() { "?".into() } else { b }));
    }
    out
}
