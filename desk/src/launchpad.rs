//! Launchpad: oracle-desk is THE platform executable — everything starts from
//! here. These helpers spawn the ecosystem's components cross-platform.

use std::path::Path;
use std::process::Command;

fn ps(root: &Path) -> Command {
    let mut c = Command::new("powershell");
    c.args(["-NoProfile", "-ExecutionPolicy", "Bypass"]);
    c.current_dir(root);
    c
}

/// Open a script in a NEW terminal window (interactive sessions need a console).
pub fn open_terminal(root: &Path, script_rel: &str, extra: &[&str]) {
    if cfg!(windows) {
        let script = root.join(script_rel).display().to_string();
        let mut args = vec![
            "/C".into(), "start".into(), "SentiVue Oracle".into(),
            "powershell".into(), "-NoExit".into(), "-ExecutionPolicy".into(),
            "Bypass".into(), "-File".into(), script,
        ];
        args.extend(extra.iter().map(|s| s.to_string()));
        let _ = Command::new("cmd").args(&args).current_dir(root).spawn();
    } else {
        let cmdline = format!(
            "cd {} && bash {} {}",
            root.display(),
            script_rel,
            extra.join(" ")
        );
        let script = format!("tell application \"Terminal\" to do script \"{cmdline}\"");
        let _ = Command::new("osascript").args(["-e", &script]).spawn();
        let _ = Command::new("osascript")
            .args(["-e", "tell application \"Terminal\" to activate"])
            .spawn();
    }
}

pub fn serve(root: &Path, start: bool) {
    let verb = if start { "start" } else { "stop" };
    if cfg!(windows) {
        let _ = ps(root)
            .args(["-WindowStyle", "Hidden", "-File"])
            .arg(root.join("serving/serve-windows.ps1"))
            .arg(verb)
            .spawn();
    } else {
        let _ = Command::new("bash")
            .arg(root.join("serving/service.sh"))
            .arg(verb)
            .current_dir(root)
            .spawn();
    }
}

pub fn open_ide(root: &Path) {
    if cfg!(windows) {
        let _ = ps(root)
            .args(["-WindowStyle", "Hidden", "-File"])
            .arg(root.join("connectors/ide/setup-ide.ps1"))
            .arg("launch")
            .spawn();
    } else {
        let _ = Command::new("bash")
            .arg(root.join("connectors/ide/setup-ide.sh"))
            .arg("launch")
            .current_dir(root)
            .spawn();
    }
}

pub fn claude_session(root: &Path) {
    if cfg!(windows) {
        open_terminal(root, "engines/claude-code/launch.ps1", &[]);
    } else {
        open_terminal(root, "engines/claude-code/launch.sh", &[]);
    }
}

pub fn opencode_session(root: &Path) {
    if cfg!(windows) {
        open_terminal(root, "engines/opencode/launch.ps1", &[]);
    } else {
        open_terminal(root, "engines/opencode/launch.sh", &[]);
    }
}

pub fn envoy_window(root: &Path) {
    if cfg!(windows) {
        // Envoy windows are macOS-first (pf air-gap); on Windows just open the queue.
        let _ = Command::new("notepad")
            .arg(root.join("memory/NET-REQUESTS.md"))
            .spawn();
    } else {
        open_terminal(root, "bootstrap/envoy.sh", &[]);
    }
}

pub fn vault_sync_window(root: &Path) {
    if cfg!(windows) {
        open_terminal(root, "bootstrap/vault.ps1", &["sync"]);
    } else {
        open_terminal(root, "bootstrap/vault.sh", &["sync"]);
    }
}

pub fn first_time_setup(root: &Path) {
    if cfg!(windows) {
        open_terminal(root, "bin/oracle.ps1", &["setup"]);
    } else {
        open_terminal(root, "install", &[]);
    }
}

// ---- readiness probes (drive which buttons show) ----

pub fn engines_ready(root: &Path) -> bool {
    if cfg!(windows) {
        root.join(".tools/npm/claude.cmd").exists() || root.join(".tools/npm/claude").exists()
    } else {
        root.join(".tools/npm/bin/claude").exists()
    }
}

pub fn serving_ready(root: &Path) -> bool {
    if cfg!(windows) {
        root.join(".tools/win/llama-swap.exe").exists()
    } else {
        root.join(".tools/bin/llama-swap").exists()
    }
}

pub fn models_present(root: &Path) -> bool {
    std::fs::read_dir(root.join("models"))
        .map(|rd| {
            rd.flatten().any(|e| {
                std::fs::read_dir(e.path())
                    .map(|d| d.flatten().any(|f| {
                        f.path().extension().map(|x| x == "gguf").unwrap_or(false)
                            || f.path().is_dir()
                    }))
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false)
}

pub fn download_models_window(root: &Path) {
    if cfg!(windows) {
        open_terminal(root, "bootstrap/download-models.ps1", &[]);
    } else {
        open_terminal(root, "bootstrap/download-models.sh", &[]);
    }
}
