//! Links dev servers to the Claude Code or Codex session that started them.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Serialize)]
pub struct AgentSession {
    pub kind: &'static str,
    pub id: String,
    pub title: Option<String>,
}

#[derive(Default)]
pub struct AgentResolver {
    claude: HashMap<String, (AgentSession, f64)>,
    codex: HashMap<String, AgentSession>,
    codex_indexed_at: f64,
}

impl AgentResolver {
    pub fn resolve(
        &mut self,
        env: &HashMap<String, String>,
        cwd: Option<&str>,
        link_claude: bool,
        link_codex: bool,
    ) -> Option<AgentSession> {
        let claude_id = env.get("CLAUDE_CODE_SESSION_ID");
        if link_claude && let Some(id) = claude_id {
            return Some(self.claude_session(id));
        }
        if !link_codex || claude_id.is_some() {
            return None;
        }
        self.refresh_codex();
        let home = crate::home_dir();
        let mut dir = Some(Path::new(cwd?));
        while let Some(d) = dir {
            if d.as_os_str().len() <= home.as_os_str().len() {
                break;
            }
            if let Some(s) = d.to_str().and_then(|k| self.codex.get(k)) {
                return Some(s.clone());
            }
            dir = d.parent();
        }
        None
    }

    fn claude_session(&mut self, id: &str) -> AgentSession {
        let now = crate::now();
        if let Some((s, _)) = self.claude.get(id).filter(|(_, at)| now - at < 30.0) {
            return s.clone();
        }
        let title = find_claude_transcript(id).and_then(|path| {
            let head = read_head(&path, 256 * 1024);
            let tail = read_tail(&path, 512 * 1024);
            last_title(&tail).or_else(|| last_title(&head)).or_else(|| first_prompt(&head))
        });
        let session = AgentSession { kind: "Claude Code", id: id.into(), title };
        self.claude.insert(id.into(), (session.clone(), now));
        session
    }

    fn refresh_codex(&mut self) {
        let now = crate::now();
        if now - self.codex_indexed_at <= 60.0 {
            return;
        }
        self.codex_indexed_at = now;
        let codex = crate::home_dir().join(".codex");

        let mut titles: HashMap<String, String> = HashMap::new();
        for line in read_tail(&codex.join("session_index.jsonl"), 1024 * 1024).lines() {
            if let Ok(v) = serde_json::from_str::<Value>(line)
                && let (Some(id), Some(name)) = (v["id"].as_str(), v["thread_name"].as_str())
            {
                titles.insert(id.into(), name.into());
            }
        }

        let cutoff = now - 3.0 * 86400.0;
        let mut index: HashMap<String, (AgentSession, f64)> = HashMap::new();
        let mut stack = vec![codex.join("sessions")];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir).into_iter().flatten().flatten() {
                let path = entry.path();
                let Ok(meta) = entry.metadata() else { continue };
                if meta.is_dir() {
                    stack.push(path);
                    continue;
                }
                if path.extension().is_none_or(|e| e != "jsonl") {
                    continue;
                }
                let mtime = meta
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map_or(0.0, |d| d.as_secs_f64());
                if mtime < cutoff {
                    continue;
                }
                let head = read_head(&path, 64 * 1024);
                let Some(first) = head.lines().next() else { continue };
                let Ok(v) = serde_json::from_str::<Value>(first) else { continue };
                let (Some(id), Some(cwd)) = (v["payload"]["id"].as_str(), v["payload"]["cwd"].as_str()) else {
                    continue;
                };
                if index.get(cwd).is_some_and(|(_, t)| *t > mtime) {
                    continue;
                }
                let session = AgentSession { kind: "Codex", id: id.into(), title: titles.get(id).cloned() };
                index.insert(cwd.into(), (session, mtime));
            }
        }
        self.codex = index.into_iter().map(|(k, (s, _))| (k, s)).collect();
    }
}

fn find_claude_transcript(id: &str) -> Option<PathBuf> {
    let projects = crate::home_dir().join(".claude/projects");
    fs::read_dir(projects)
        .ok()?
        .flatten()
        .map(|e| e.path().join(format!("{id}.jsonl")))
        .find(|p| p.is_file())
}

fn last_title(text: &str) -> Option<String> {
    let mut title = None;
    for line in text.lines() {
        if !(line.contains(r#""type":"custom-title""#) || line.contains(r#""type":"ai-title""#)) {
            continue;
        }
        let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
        if let Some(custom) = v["customTitle"].as_str().filter(|s| !s.is_empty()) {
            return Some(custom.into());
        }
        if let Some(ai) = v["aiTitle"].as_str().filter(|s| !s.is_empty()) {
            title = Some(ai.into());
        }
    }
    title
}

fn first_prompt(text: &str) -> Option<String> {
    text.lines()
        .filter(|l| l.contains(r#""type":"user""#))
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .find_map(|v| {
            let content = v["message"]["content"].as_str()?.trim().to_string();
            (!content.is_empty() && !content.starts_with('<'))
                .then(|| content.lines().next().unwrap_or("").chars().take(60).collect())
        })
}

fn read_head(path: &Path, max: u64) -> String {
    let mut buf = Vec::new();
    if let Ok(f) = File::open(path) {
        let _ = f.take(max).read_to_end(&mut buf);
    }
    String::from_utf8_lossy(&buf).into_owned()
}

fn read_tail(path: &Path, max: u64) -> String {
    let Ok(mut f) = File::open(path) else { return String::new() };
    let size = f.metadata().map_or(0, |m| m.len());
    let offset = size.saturating_sub(max);
    let mut buf = Vec::new();
    if f.seek(SeekFrom::Start(offset)).is_err() || f.read_to_end(&mut buf).is_err() {
        return String::new();
    }
    let text = String::from_utf8_lossy(&buf).into_owned();
    match (offset > 0, text.find('\n')) {
        (true, Some(i)) => text[i + 1..].to_string(),
        _ => text,
    }
}
