//! oracle-desk — the SentiVue Oracle native frontend.
//! Pure Rust + egui: a single binary, no webview, no browser, no servers.
//! Chat drives the engines headlessly (structured stream-json for Claude Code);
//! mission control reads the plain-text state files; models via llama-swap's
//! localhost API; vault via git.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod data;
mod engine;
mod launchpad;
mod payload;
mod theme;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use eframe::egui;
use engine::{EngineKind, Event, RunHandle};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Launch,
    Chat,
    Missions,
    Models,
    Vault,
}

struct ChatMsg {
    role: &'static str, // "you" | "oracle" | "tool" | "system"
    text: String,
}

struct DeskApp {
    root: PathBuf,
    installed: bool,             // false => first-run self-extraction screen
    install_dest: String,
    install_msg: Option<String>,
    tab: Tab,
    // chat
    engine_kind: EngineKind,
    input: String,
    messages: Vec<ChatMsg>,
    run: Option<RunHandle>,
    session_id: Option<String>,
    streaming: String,
    // cached panels
    last_refresh: Instant,
    state_md: String,
    ledger_tail: String,
    netreq_tail: String,
    approvals: Vec<String>,
    reports: Vec<PathBuf>,
    report_view: Option<(String, String)>,
    models: Option<data::ModelStatus>,
    vault_text: String,
}

impl DeskApp {
    fn new() -> Self {
        let found = data::find_root();
        let installed = found.is_some();
        let root = found.unwrap_or_else(data::default_install_dir);
        let mut app = Self {
            install_dest: root.display().to_string(),
            install_msg: None,
            installed,
            root,
            tab: Tab::Launch,
            engine_kind: EngineKind::Claude,
            input: String::new(),
            messages: vec![ChatMsg {
                role: "system",
                text: "Engines run on local models only. Pick an engine, type, Enter to send. \
                       Multi-turn context is kept per engine session."
                    .into(),
            }],
            run: None,
            session_id: None,
            streaming: String::new(),
            last_refresh: Instant::now() - Duration::from_secs(60),
            state_md: String::new(),
            ledger_tail: String::new(),
            netreq_tail: String::new(),
            approvals: Vec::new(),
            reports: Vec::new(),
            report_view: None,
            models: None,
            vault_text: String::new(),
        };
        if app.installed {
            app.refresh();
        }
        app
    }

    fn install_ui(&mut self, ui: &mut egui::Ui) {
        ui.add_space(40.0);
        ui.vertical_centered(|ui| {
            ui.heading("SentiVue Oracle");
            ui.label("self-contained development ecosystem — this executable IS the platform");
            ui.add_space(20.0);
            if payload::has_payload() {
                ui.label("Install the platform to:");
                ui.add(egui::TextEdit::singleline(&mut self.install_dest).desired_width(420.0));
                ui.add_space(8.0);
                if ui.button("Install here (self-extract, ~1 s)").clicked() {
                    let dest = PathBuf::from(self.install_dest.trim());
                    match payload::extract_to(&dest) {
                        Ok(n) => {
                            self.install_msg = Some(format!("installed {n} files"));
                            self.root = dest;
                            self.installed = true;
                            self.refresh();
                        }
                        Err(e) => self.install_msg = Some(format!("failed: {e}")),
                    }
                }
            } else {
                ui.label("This build carries no embedded payload; place the exe inside a platform checkout.");
            }
            if let Some(m) = &self.install_msg {
                ui.add_space(6.0);
                ui.label(m.as_str());
            }
        });
    }

    fn refresh(&mut self) {
        let r = &self.root;
        self.state_md = data::read_tail(&r.join("memory/STATE.md"), 60);
        self.ledger_tail = data::read_tail(&r.join("memory/LEDGER.md"), 25);
        self.netreq_tail = data::read_tail(&r.join("memory/NET-REQUESTS.md"), 20);
        self.approvals = data::awaiting_approvals(r);
        self.reports = data::list_reports(r);
        self.models = Some(data::model_status());
        self.vault_text = data::vault_inventory(r);
        self.last_refresh = Instant::now();
    }

    fn pump_engine(&mut self) {
        let Some(run) = &self.run else { return };
        let mut done = false;
        while let Ok(ev) = run.rx.try_recv() {
            match ev {
                Event::Text(t) => self.streaming.push_str(&t),
                Event::Tool(name) => {
                    if !self.streaming.is_empty() {
                        self.messages.push(ChatMsg { role: "oracle", text: std::mem::take(&mut self.streaming) });
                    }
                    self.messages.push(ChatMsg { role: "tool", text: format!("[{name}]") });
                }
                Event::Done { session_id } => {
                    if session_id.is_some() {
                        self.session_id = session_id;
                    }
                    done = true;
                }
                Event::Fail(msg) => {
                    self.messages.push(ChatMsg { role: "system", text: msg });
                    done = true;
                }
            }
        }
        if done {
            if !self.streaming.is_empty() {
                self.messages.push(ChatMsg { role: "oracle", text: std::mem::take(&mut self.streaming) });
            }
            self.run = None;
        }
    }

    fn send_prompt(&mut self) {
        let prompt = self.input.trim().to_string();
        if prompt.is_empty() || self.run.is_some() {
            return;
        }
        self.input.clear();
        self.messages.push(ChatMsg { role: "you", text: prompt.clone() });
        self.run = Some(engine::send(
            self.root.clone(),
            self.engine_kind,
            self.session_id.clone(),
            prompt,
        ));
    }

    // ---------------- panels ----------------

    fn launch_ui(&mut self, ui: &mut egui::Ui) {
        let healthy = self.models.as_ref().map(|m| m.healthy).unwrap_or(false);
        let engines = launchpad::engines_ready(&self.root);
        let serving = launchpad::serving_ready(&self.root);
        let models = launchpad::models_present(&self.root);

        ui.add_space(8.0);
        ui.heading(egui::RichText::new("✻ SentiVue Oracle — the whole platform from here").color(theme::ORANGE));
        ui.add_space(4.0);
        ui.horizontal(|ui| {
            let dot = |ok: bool| if ok { theme::GREEN } else { theme::RED };
            ui.colored_label(dot(healthy), "●"); ui.label("serving");
            ui.separator();
            ui.colored_label(dot(engines), "●"); ui.label("engines");
            ui.separator();
            ui.colored_label(dot(models), "●"); ui.label("models");
        });
        ui.add_space(10.0);

        if !engines || !serving {
            ui.label("First-time setup installs the engines and serving toolchain (one time):");
            if ui.button("⚙  Run first-time setup").clicked() {
                launchpad::first_time_setup(&self.root);
            }
            ui.add_space(8.0);
            ui.separator();
        }
        if !models {
            ui.label("No models downloaded yet (profile-aware, resumable):");
            if ui.button("⇩  Download models").clicked() {
                launchpad::download_models_window(&self.root);
            }
            ui.add_space(8.0);
            ui.separator();
        }

        egui::Grid::new("launch_grid").num_columns(2).spacing([14.0, 12.0]).show(ui, |ui| {
            if healthy {
                if ui.button("■  Stop model serving").clicked() { launchpad::serve(&self.root, false); }
            } else if ui.button("▶  Start model serving").clicked() {
                launchpad::serve(&self.root, true);
            }
            if ui.button("⌨  IDE (Cursor-like, local models)").clicked() { launchpad::open_ide(&self.root); }
            ui.end_row();
            if ui.button("✦  Claude Code session").clicked() { launchpad::claude_session(&self.root); }
            if ui.button("✦  OpenCode session").clicked() { launchpad::opencode_session(&self.root); }
            ui.end_row();
            if ui.button("⇄  Vault sync").clicked() { launchpad::vault_sync_window(&self.root); }
            if ui.button("☷  Envoy / network queue").clicked() { launchpad::envoy_window(&self.root); }
            ui.end_row();
        });

        ui.add_space(12.0);
        ui.separator();
        ui.label("Chat in the next tab talks to the engines directly; Missions shows the");
        ui.label("autonomous loop with one-click approvals. Everything runs on local models.");
    }

    fn chat_ui(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label("engine:");
            let before = self.engine_kind;
            ui.selectable_value(&mut self.engine_kind, EngineKind::Claude, EngineKind::Claude.label());
            ui.selectable_value(&mut self.engine_kind, EngineKind::OpenCode, EngineKind::OpenCode.label());
            if before != self.engine_kind {
                self.session_id = None; // sessions do not cross engines
            }
            if self.session_id.is_some() && ui.button("new session").clicked() {
                self.session_id = None;
                self.messages.push(ChatMsg { role: "system", text: "-- new session --".into() });
            }
            if self.run.is_some() {
                ui.spinner();
                if ui.button("stop").clicked() {
                    if let Some(run) = &self.run {
                        run.stop();
                    }
                }
            }
        });
        ui.separator();
        let avail = ui.available_height() - 70.0;
        egui::ScrollArea::vertical()
            .max_height(avail)
            .stick_to_bottom(true)
            .show(ui, |ui| {
                for m in &self.messages {
                    match m.role {
                        "you" => {
                            ui.horizontal_wrapped(|ui| {
                                ui.colored_label(theme::CORAL, "❯");
                                ui.label(m.text.as_str());
                            });
                        }
                        "tool" => {
                            ui.horizontal_wrapped(|ui| {
                                ui.colored_label(theme::GREEN, "⏺");
                                ui.colored_label(theme::DIM, m.text.trim_matches(['[', ']']));
                            });
                        }
                        "oracle" => {
                            ui.horizontal_wrapped(|ui| {
                                ui.colored_label(theme::CORAL, "✻");
                                ui.label(m.text.as_str());
                            });
                        }
                        _ => {
                            ui.colored_label(theme::DIM, m.text.as_str());
                        }
                    }
                    ui.add_space(6.0);
                }
                if !self.streaming.is_empty() {
                    ui.horizontal_wrapped(|ui| {
                        ui.colored_label(theme::CORAL, "✻");
                        ui.label(self.streaming.as_str());
                    });
                }
            });
        ui.separator();
        egui::Frame::none()
            .fill(theme::BG_DEEP)
            .stroke(egui::Stroke::new(1.0, theme::BORDER))
            .rounding(egui::Rounding::same(6.0))
            .inner_margin(egui::Margin::symmetric(10.0, 8.0))
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.colored_label(theme::CORAL, "❯");
                    let editor = egui::TextEdit::singleline(&mut self.input)
                        .hint_text("ask the oracle…")
                        .frame(false)
                        .desired_width(ui.available_width() - 40.0);
                    let resp = ui.add(editor);
                    let send_clicked = ui.button("⏎").clicked();
                    if send_clicked
                        || (resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)))
                    {
                        self.send_prompt();
                        resp.request_focus();
                    }
                });
            });
    }

    fn missions_ui(&mut self, ui: &mut egui::Ui) {
        egui::ScrollArea::vertical().show(ui, |ui| {
            if !self.approvals.is_empty() {
                ui.heading("awaiting your countersign");
                let ids = self.approvals.clone();
                for id in ids {
                    ui.horizontal(|ui| {
                        ui.monospace(id.as_str());
                        if ui.button(egui::RichText::new("APPROVE").color(theme::ORANGE)).clicked() {
                            data::approve(&self.root, &id);
                            self.last_refresh = Instant::now() - Duration::from_secs(60);
                        }
                    });
                }
                ui.separator();
            }
            ui.heading("mission state");
            ui.monospace(self.state_md.as_str());
            ui.separator();
            ui.heading("network requests (envoy queue)");
            ui.monospace(self.netreq_tail.as_str());
            ui.separator();
            ui.heading("ledger");
            ui.monospace(self.ledger_tail.as_str());
            ui.separator();
            ui.heading("reports");
            let reports = self.reports.clone();
            for p in reports {
                let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("?").to_string();
                if ui.link(name.clone()).clicked() {
                    let body = std::fs::read_to_string(&p).unwrap_or_default();
                    self.report_view = Some((name.clone(), body));
                }
            }
        });
        let mut close = false;
        if let Some((name, body)) = &self.report_view {
            egui::Window::new(name.clone())
                .default_width(720.0)
                .default_height(520.0)
                .show(ui.ctx(), |ui| {
                    egui::ScrollArea::vertical().show(ui, |ui| ui.monospace(body.as_str()));
                    if ui.button("close").clicked() {
                        close = true;
                    }
                });
        }
        if close {
            self.report_view = None;
        }
    }

    fn models_ui(&mut self, ui: &mut egui::Ui) {
        if let Some(m) = &self.models {
            let (dot, label) = if m.healthy {
                (theme::GREEN, "llama-swap: healthy")
            } else {
                (theme::RED, "llama-swap: down (oracle serve)")
            };
            ui.horizontal(|ui| {
                ui.colored_label(dot, "●");
                ui.label(label);
            });
            ui.separator();
            ui.heading("resident / running");
            ui.monospace(m.running.as_str());
        }
        ui.separator();
        ui.label("tiers: opus → kimi-k2-thinking · sonnet → qwen3-coder-480b · haiku → qwen3-coder-30b");
        ui.label("big-slot swaps take 1–2 min from SSD; the fast lane never unloads.");
    }

    fn vault_ui(&mut self, ui: &mut egui::Ui) {
        egui::ScrollArea::vertical().show(ui, |ui| {
            ui.monospace(self.vault_text.as_str());
        });
    }
}

impl eframe::App for DeskApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if !self.installed {
            egui::CentralPanel::default().show(ctx, |ui| self.install_ui(ui));
            return;
        }
        self.pump_engine();
        if self.last_refresh.elapsed() > Duration::from_secs(3) {
            self.refresh();
        }
        ctx.request_repaint_after(Duration::from_millis(250));

        egui::TopBottomPanel::top("tabs").show(ctx, |ui| {
            ui.add_space(2.0);
            ui.horizontal(|ui| {
                ui.colored_label(theme::CORAL, egui::RichText::new("✻").heading());
                ui.heading(egui::RichText::new("✻ SentiVue Oracle").color(theme::ORANGE));
                ui.separator();
                ui.selectable_value(&mut self.tab, Tab::Launch, "Launch");
                ui.selectable_value(&mut self.tab, Tab::Chat, "Chat");
                ui.selectable_value(&mut self.tab, Tab::Missions, "Missions");
                ui.selectable_value(&mut self.tab, Tab::Models, "Models");
                ui.selectable_value(&mut self.tab, Tab::Vault, "Vault");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(
                        egui::RichText::new(self.root.display().to_string())
                            .small()
                            .color(theme::DIM),
                    );
                });
            });
            ui.add_space(2.0);
        });

        egui::TopBottomPanel::bottom("statusline").show(ctx, |ui| {
            ui.horizontal(|ui| {
                let healthy = self.models.as_ref().map(|m| m.healthy).unwrap_or(false);
                let (dot, txt) = if healthy { (theme::GREEN, "serving") } else { (theme::DIM, "offline") };
                ui.colored_label(dot, "●");
                ui.colored_label(theme::DIM, txt);
                ui.separator();
                ui.colored_label(theme::DIM, self.engine_kind.label());
                if let Some(sid) = &self.session_id {
                    ui.separator();
                    let short: String = sid.chars().take(8).collect();
                    ui.colored_label(theme::DIM, format!("session {short}"));
                }
            });
        });

        egui::CentralPanel::default().show(ctx, |ui| match self.tab {
            Tab::Launch => self.launch_ui(ui),
            Tab::Chat => self.chat_ui(ui),
            Tab::Missions => self.missions_ui(ui),
            Tab::Models => self.models_ui(ui),
            Tab::Vault => self.vault_ui(ui),
        });
    }
}

fn main() -> eframe::Result {
    // Headless self-extraction for scripts/installers: oracle-desk --extract-to <dir>
    let args: Vec<String> = std::env::args().collect();
    if let Some(i) = args.iter().position(|a| a == "--extract-to") {
        if let Some(dir) = args.get(i + 1) {
            match payload::extract_to(std::path::Path::new(dir)) {
                Ok(n) => {
                    println!("extracted {n} files to {dir}");
                    std::process::exit(0);
                }
                Err(e) => {
                    eprintln!("extract failed: {e}");
                    std::process::exit(1);
                }
            }
        }
    }
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1280.0, 860.0])
            .with_title("SentiVue Oracle"),
        ..Default::default()
    };
    eframe::run_native(
        "SentiVue Oracle",
        options,
        Box::new(|cc| {
            theme::apply(&cc.egui_ctx);
            Ok(Box::new(DeskApp::new()))
        }),
    )
}
