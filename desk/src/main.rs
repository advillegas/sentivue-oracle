//! oracle-desk — the SentiVue Oracle native frontend.
//! Pure Rust + egui: a single binary, no webview, no browser, no servers.
//! Chat drives the engines headlessly (structured stream-json for Claude Code);
//! mission control reads the plain-text state files; models via llama-swap's
//! localhost API; vault via git.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod data;
mod engine;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use eframe::egui;
use engine::{EngineKind, Event, RunHandle};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
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
        let root = data::find_root();
        let mut app = Self {
            root,
            tab: Tab::Chat,
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
        app.refresh();
        app
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
                    let (tag, color) = match m.role {
                        "you" => ("you", egui::Color32::from_rgb(88, 166, 255)),
                        "oracle" => ("oracle", egui::Color32::from_rgb(63, 185, 80)),
                        "tool" => ("tool", egui::Color32::from_rgb(210, 153, 34)),
                        _ => ("system", egui::Color32::GRAY),
                    };
                    ui.horizontal_wrapped(|ui| {
                        ui.colored_label(color, format!("{tag} >"));
                        ui.label(m.text.as_str());
                    });
                    ui.add_space(4.0);
                }
                if !self.streaming.is_empty() {
                    ui.horizontal_wrapped(|ui| {
                        ui.colored_label(egui::Color32::from_rgb(63, 185, 80), "oracle >");
                        ui.label(self.streaming.as_str());
                    });
                }
            });
        ui.separator();
        ui.horizontal(|ui| {
            let editor = egui::TextEdit::singleline(&mut self.input)
                .hint_text("ask the oracle…")
                .desired_width(ui.available_width() - 70.0);
            let resp = ui.add(editor);
            let send_clicked = ui.button("send").clicked();
            if send_clicked || (resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter))) {
                self.send_prompt();
                resp.request_focus();
            }
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
                        if ui.button("APPROVE").clicked() {
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
                (egui::Color32::from_rgb(63, 185, 80), "llama-swap: healthy")
            } else {
                (egui::Color32::from_rgb(248, 81, 73), "llama-swap: down (oracle serve)")
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
        self.pump_engine();
        if self.last_refresh.elapsed() > Duration::from_secs(3) {
            self.refresh();
        }
        ctx.request_repaint_after(Duration::from_millis(250));

        egui::TopBottomPanel::top("tabs").show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.heading("SentiVue Oracle");
                ui.separator();
                ui.selectable_value(&mut self.tab, Tab::Chat, "Chat");
                ui.selectable_value(&mut self.tab, Tab::Missions, "Missions");
                ui.selectable_value(&mut self.tab, Tab::Models, "Models");
                ui.selectable_value(&mut self.tab, Tab::Vault, "Vault");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(
                        egui::RichText::new(self.root.display().to_string())
                            .small()
                            .color(egui::Color32::GRAY),
                    );
                });
            });
        });

        egui::CentralPanel::default().show(ctx, |ui| match self.tab {
            Tab::Chat => self.chat_ui(ui),
            Tab::Missions => self.missions_ui(ui),
            Tab::Models => self.models_ui(ui),
            Tab::Vault => self.vault_ui(ui),
        });
    }
}

fn main() -> eframe::Result {
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
            cc.egui_ctx.set_visuals(egui::Visuals::dark());
            Ok(Box::new(DeskApp::new()))
        }),
    )
}
