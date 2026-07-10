//! Assistant tab backend: the platform as a functional LLM. Talks directly to
//! llama-swap's OpenAI endpoint (streaming SSE) — no coding harness in the
//! path, so it behaves like a general chat assistant, entirely local.

use std::io::{BufRead, BufReader};
use std::sync::mpsc::{channel, Receiver};
use std::thread;

pub enum Event {
    Delta(String),
    Done,
    Fail(String),
}

pub const SYSTEM: &str = "You are Oracle, a capable general assistant running entirely on \
local models on this machine — nothing you are told ever leaves it. Be direct, expert, \
and concise. You can discuss anything: engineering, quantitative research, mathematics, \
writing, or general knowledge. Use markdown sparingly (this is a plain-text view).";

pub fn send(model: String, messages: Vec<(String, String)>) -> Receiver<Event> {
    let (tx, rx) = channel::<Event>();
    thread::spawn(move || {
        let msgs: Vec<serde_json::Value> = messages
            .iter()
            .map(|(role, content)| serde_json::json!({ "role": role, "content": content }))
            .collect();
        let body = serde_json::json!({ "model": model, "stream": true, "messages": msgs });
        let resp = ureq::post("http://127.0.0.1:9099/v1/chat/completions")
            .timeout(std::time::Duration::from_secs(600))
            .send_json(body);
        let resp = match resp {
            Ok(r) => r,
            Err(e) => {
                let _ = tx.send(Event::Fail(format!(
                    "model endpoint not answering ({e}) — start serving from the Launch tab"
                )));
                return;
            }
        };
        for line in BufReader::new(resp.into_reader()).lines() {
            let Ok(line) = line else { break };
            let line = line.trim();
            let Some(data) = line.strip_prefix("data: ") else { continue };
            if data == "[DONE]" {
                break;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(data) {
                if let Some(t) = v.pointer("/choices/0/delta/content").and_then(|x| x.as_str()) {
                    if !t.is_empty() && tx.send(Event::Delta(t.to_string())).is_err() {
                        return;
                    }
                }
            }
        }
        let _ = tx.send(Event::Done);
    });
    rx
}
