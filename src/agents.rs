//! Links dev servers to the Claude Code or Codex session that started them.
//!
//! Claude Code publishes `CLAUDE_CODE_SESSION_ID`. Codex publishes
//! `CODEX_THREAD_ID` (and `CODEX_SESSION_ID`) on commands it runs. Without
//! those, a Codex chat is chosen from the working folder by the process start
//! time and, when several chats overlap, by command text in the transcript.
//! A link that only has the folder to go on is marked estimated.

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
    /// True when the chat was inferred from the working folder, not identified.
    pub estimated: bool,
}

#[derive(Default)]
pub struct AgentResolver {
    claude: HashMap<String, (AgentSession, f64)>,
    codex: Vec<CodexThread>,
    codex_titles: HashMap<String, String>,
    codex_indexed_at: f64,
    transcript_hits: HashMap<(PathBuf, String), bool>,
    /// Test override. Production uses `~/.codex`.
    codex_home: Option<PathBuf>,
}

#[derive(Clone)]
struct CodexThread {
    id: String,
    session_id: String,
    cwd: String,
    title: Option<String>,
    started_at: Option<f64>,
    mtime: f64,
    path: PathBuf,
}

impl CodexThread {
    fn to_session(&self, estimated: bool) -> AgentSession {
        AgentSession {
            kind: "Codex",
            id: self.id.clone(),
            title: self.title.clone(),
            estimated,
        }
    }
}

impl AgentResolver {
    pub fn resolve(
        &mut self,
        env: &HashMap<String, String>,
        cwd: Option<&str>,
        started_at: f64,
        command: Option<&str>,
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
        self.match_codex(env, cwd?, started_at, command)
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
        let session = AgentSession { kind: "Claude Code", id: id.into(), title, estimated: false };
        self.claude.insert(id.into(), (session.clone(), now));
        session
    }

    fn match_codex(
        &mut self,
        env: &HashMap<String, String>,
        cwd: &str,
        started_at: f64,
        command: Option<&str>,
    ) -> Option<AgentSession> {
        // The thread id is the exact chat. It wins over a newer transcript in the same folder.
        if let Some(id) = env_id(env, "CODEX_THREAD_ID") {
            if let Some(thread) = self.codex.iter().find(|t| t.id == id) {
                return Some(thread.to_session(false));
            }
            return Some(AgentSession {
                kind: "Codex",
                id: id.to_string(),
                title: self.codex_titles.get(id).cloned(),
                estimated: false,
            });
        }

        let mut threads = self.threads_for_cwd(cwd);
        if let Some(id) = env_id(env, "CODEX_SESSION_ID") {
            let scoped: Vec<CodexThread> = threads.iter().filter(|t| t.session_id == id || t.id == id).cloned().collect();
            if scoped.len() == 1 {
                return Some(scoped[0].to_session(false));
            }
            if scoped.len() > 1 {
                threads = scoped;
            }
        }
        if threads.is_empty() {
            return None;
        }

        let before: Vec<CodexThread> = threads
            .iter()
            .filter(|t| t.started_at.is_some_and(|t0| t0 <= started_at))
            .cloned()
            .collect();
        let time_confirmed = !before.is_empty();
        let mut pool = deepest(if time_confirmed { &before } else { &threads });

        if pool.len() > 1 && let Some(cmd) = command.filter(|c| command_is_specific(c)) {
            let mentioned: Vec<CodexThread> = pool.iter().filter(|t| self.transcript_contains(&t.path, cmd)).cloned().collect();
            if mentioned.len() == 1 {
                pool = mentioned;
            }
        }

        let estimated = !(time_confirmed && pool.len() == 1);
        let chosen = if estimated {
            // Best guess: the chat opened most recently. Callers show it as estimated.
            pool.iter().max_by(|a, b| {
                a.started_at.unwrap_or(0.0).total_cmp(&b.started_at.unwrap_or(0.0)).then_with(|| a.mtime.total_cmp(&b.mtime))
            })?
        } else {
            pool.first()?
        };
        Some(chosen.to_session(estimated))
    }

    fn threads_for_cwd(&self, cwd: &str) -> Vec<CodexThread> {
        let home_len = crate::home_dir().as_os_str().len();
        let process = Path::new(cwd);
        self.codex
            .iter()
            .filter(|t| {
                let session = Path::new(&t.cwd);
                session.as_os_str().len() > home_len && (process == session || process.starts_with(session))
            })
            .cloned()
            .collect()
    }

    fn refresh_codex(&mut self) {
        let now = crate::now();
        if now - self.codex_indexed_at <= 60.0 && self.codex_indexed_at > 0.0 {
            return;
        }
        self.codex_indexed_at = now;
        self.transcript_hits.clear();
        let codex = self.codex_home.clone().unwrap_or_else(|| crate::home_dir().join(".codex"));

        let mut titles: HashMap<String, String> = HashMap::new();
        for line in read_tail(&codex.join("session_index.jsonl"), 1024 * 1024).lines() {
            if let Ok(v) = serde_json::from_str::<Value>(line)
                && let (Some(id), Some(name)) = (v["id"].as_str(), v["thread_name"].as_str())
                && !name.is_empty()
            {
                titles.insert(id.into(), name.into());
            }
        }
        self.codex_titles = titles;

        let cutoff = now - 3.0 * 86400.0;
        let mut by_id: HashMap<String, CodexThread> = HashMap::new();
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
                let Some(thread) = thread_from_file(&path, mtime, &self.codex_titles) else { continue };
                match by_id.get(&thread.id) {
                    Some(prev) if prev.mtime > thread.mtime => {}
                    _ => {
                        by_id.insert(thread.id.clone(), thread);
                    }
                }
            }
        }
        self.codex = by_id.into_values().collect();
    }

    fn transcript_contains(&mut self, path: &Path, command: &str) -> bool {
        let key = (path.to_path_buf(), command.to_string());
        if let Some(hit) = self.transcript_hits.get(&key) {
            return *hit;
        }
        let hit = file_contains(path, command);
        self.transcript_hits.insert(key, hit);
        hit
    }
}

fn env_id<'a>(env: &'a HashMap<String, String>, key: &str) -> Option<&'a str> {
    env.get(key).map(String::as_str).filter(|s| !s.is_empty())
}

fn command_is_specific(command: &str) -> bool {
    let command = command.trim();
    command.len() >= 4 && (command.contains(' ') || command.len() >= 12)
}

fn deepest(threads: &[CodexThread]) -> Vec<CodexThread> {
    let max_len = threads.iter().map(|t| t.cwd.len()).max().unwrap_or(0);
    threads.iter().filter(|t| t.cwd.len() == max_len).cloned().collect()
}

fn thread_from_file(path: &Path, mtime: f64, titles: &HashMap<String, String>) -> Option<CodexThread> {
    let head = read_head(path, 64 * 1024);
    let first = head.lines().next()?;
    let v: Value = serde_json::from_str(first).ok()?;
    let payload = &v["payload"];
    let id = payload["id"].as_str()?;
    let cwd = payload["cwd"].as_str()?;
    let session_id = payload["session_id"].as_str().unwrap_or(id);
    let started_at = payload["timestamp"]
        .as_str()
        .or_else(|| v["timestamp"].as_str())
        .and_then(parse_rfc3339)
        .or_else(|| started_at_from_filename(path));
    Some(CodexThread {
        id: id.into(),
        session_id: session_id.into(),
        cwd: cwd.into(),
        title: titles.get(id).cloned(),
        started_at,
        mtime,
        path: path.to_path_buf(),
    })
}

fn started_at_from_filename(path: &Path) -> Option<f64> {
    let name = path.file_stem()?.to_str()?;
    let rest = name.strip_prefix("rollout-")?;
    if rest.len() < 19 {
        return None;
    }
    let stamp = format!("{}T{}:{}:{}Z", &rest[..10], &rest[11..13], &rest[14..16], &rest[17..19]);
    parse_rfc3339(&stamp)
}

fn parse_rfc3339(raw: &str) -> Option<f64> {
    let s = raw.trim();
    if s.len() < 20 || s.as_bytes().get(10) != Some(&b'T') {
        return None;
    }
    let year: i32 = s.get(0..4)?.parse().ok()?;
    let month: u32 = s.get(5..7)?.parse().ok()?;
    let day: u32 = s.get(8..10)?.parse().ok()?;
    let hour: u32 = s.get(11..13)?.parse().ok()?;
    let min: u32 = s.get(14..16)?.parse().ok()?;
    let sec: u32 = s.get(17..19)?.parse().ok()?;
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) || hour > 23 || min > 59 || sec > 60 {
        return None;
    }
    let mut rest = s.get(19..)?;
    let mut frac = 0.0;
    if let Some(stripped) = rest.strip_prefix('.') {
        let digits: String = stripped.chars().take_while(|c| c.is_ascii_digit()).collect();
        if digits.is_empty() {
            return None;
        }
        let n: f64 = digits.parse().ok()?;
        frac = n / 10f64.powi(digits.len() as i32);
        rest = stripped.get(digits.len()..)?;
    }
    let offset = if rest == "Z" || rest.is_empty() {
        0
    } else if rest.starts_with('+') || rest.starts_with('-') {
        let sign = if rest.starts_with('-') { -1 } else { 1 };
        let off = &rest[1..];
        let (h, m) = if off.len() >= 5 && off.as_bytes().get(2) == Some(&b':') {
            (off.get(0..2)?.parse::<i64>().ok()?, off.get(3..5)?.parse::<i64>().ok()?)
        } else if off.len() >= 4 {
            (off.get(0..2)?.parse::<i64>().ok()?, off.get(2..4)?.parse::<i64>().ok()?)
        } else {
            return None;
        };
        if h > 23 || m > 59 {
            return None;
        }
        sign * (h * 3600 + m * 60)
    } else {
        return None;
    };
    let days = days_from_civil(year, month, day);
    Some(days as f64 * 86400.0 + hour as f64 * 3600.0 + min as f64 * 60.0 + sec as f64 + frac - offset as f64)
}

/// Days since 1970-01-01. Howard Hinnant's `days_from_civil`.
fn days_from_civil(mut y: i32, m: u32, d: u32) -> i64 {
    y -= i32::from(m <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let month = m as i32;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + d as i32 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy as u64;
    era as i64 * 146097 + doe as i64 - 719468
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

fn file_contains(path: &Path, needle: &str) -> bool {
    let Ok(mut file) = File::open(path) else { return false };
    let needle = needle.as_bytes();
    if needle.is_empty() {
        return false;
    }
    let mut buf = vec![0u8; 64 * 1024];
    let mut carry = Vec::new();
    let mut left = 8 * 1024 * 1024u64;
    while left > 0 {
        let want = buf.len().min(left as usize);
        let n = match file.read(&mut buf[..want]) {
            Ok(0) => break,
            Ok(n) => n,
            Err(_) => break,
        };
        left -= n as u64;
        carry.extend_from_slice(&buf[..n]);
        if carry.windows(needle.len()).any(|w| w == needle) {
            return true;
        }
        let keep = needle.len() - 1;
        if carry.len() > keep {
            carry.drain(..carry.len() - keep);
        }
    }
    false
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

#[cfg(test)]
mod tests {
    use super::*;

    const CWD: &str = "/workspace/wtp-codex-fixture/app";
    const A_START: f64 = 1_792_458_000.0; // 2026-10-20T01:00:00Z
    const B_START: f64 = 1_792_461_600.0; // 2026-10-20T02:00:00Z
    const BETWEEN: f64 = 1_792_459_800.0; // 2026-10-20T01:30:00Z
    const AFTER_BOTH: f64 = 1_792_465_200.0; // 2026-10-20T03:00:00Z

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| ((*k).into(), (*v).into())).collect()
    }

    fn fixture(name: &str) -> (PathBuf, AgentResolver) {
        let root = std::env::temp_dir().join(format!("wtp-codex-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let codex = root.join(".codex");
        let sessions = codex.join("sessions/2026/10/20");
        fs::create_dir_all(&sessions).unwrap();
        fs::write(
            codex.join("session_index.jsonl"),
            "\
{\"id\":\"thread-a\",\"thread_name\":\"Fix OAuth callback\",\"updated_at\":\"2026-10-20T01:05:00Z\"}\n\
{\"id\":\"thread-b\",\"thread_name\":\"Migrate docs to Astro 5\",\"updated_at\":\"2026-10-20T02:05:00Z\"}\n",
        )
        .unwrap();
        write_rollout(
            &sessions.join("rollout-2026-10-20T01-00-00-thread-a.jsonl"),
            "thread-a",
            "2026-10-20T01:00:00.000Z",
            "{\"type\":\"response_item\",\"payload\":{\"type\":\"function_call\",\"arguments\":\"{\\\"cmd\\\":\\\"next dev --port 3000\\\"}\"}}\n",
        );
        write_rollout(&sessions.join("rollout-2026-10-20T02-00-00-thread-b.jsonl"), "thread-b", "2026-10-20T02:00:00.000Z", "");
        let resolver = AgentResolver { codex_home: Some(codex), ..Default::default() };
        (root, resolver)
    }

    fn write_rollout(path: &Path, id: &str, timestamp: &str, extra: &str) {
        let meta = format!(
            "{{\"timestamp\":\"{timestamp}\",\"type\":\"session_meta\",\"payload\":{{\"id\":\"{id}\",\"session_id\":\"{id}\",\"cwd\":\"{CWD}\",\"timestamp\":\"{timestamp}\"}}}}\n{extra}"
        );
        fs::write(path, meta).unwrap();
    }

    #[test]
    fn rfc3339_epoch() {
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00Z"), Some(0.0));
        assert_eq!(parse_rfc3339("1970-01-01T01:00:00+01:00"), Some(0.0));
        assert!((parse_rfc3339("1970-01-01T00:00:01.5Z").unwrap() - 1.5).abs() < 1e-9);
        assert_eq!(parse_rfc3339("2026-10-20T01:00:00Z"), Some(A_START));
        assert_eq!(parse_rfc3339("2026-10-20T02:00:00Z"), Some(B_START));
        assert_eq!(parse_rfc3339("not-a-time"), None);
    }

    #[test]
    fn two_codex_chats_in_the_same_folder() {
        let (root, mut resolver) = fixture("two-chats");
        let none = env(&[]);

        // Chat A started the server; chat B later worked in the same folder.
        // The process start falls in A's lifetime, so B's newer transcript must not win.
        let by_time = resolver.resolve(&none, Some(CWD), BETWEEN, None, true, true).unwrap();
        assert_eq!(by_time.id, "thread-a");
        assert_eq!(by_time.title.as_deref(), Some("Fix OAuth callback"));
        assert!(!by_time.estimated);

        // The thread id on the process is exact, even when the timestamps would say otherwise.
        let by_thread = resolver
            .resolve(&env(&[("CODEX_THREAD_ID", "thread-b")]), Some(CWD), BETWEEN, None, true, true)
            .unwrap();
        assert_eq!(by_thread.id, "thread-b");
        assert_eq!(by_thread.title.as_deref(), Some("Migrate docs to Astro 5"));
        assert!(!by_thread.estimated);

        let by_session = resolver
            .resolve(&env(&[("CODEX_SESSION_ID", "thread-a")]), Some(CWD), AFTER_BOTH, None, true, true)
            .unwrap();
        assert_eq!(by_session.id, "thread-a");
        assert!(!by_session.estimated);

        // Thread id beats a different session id (a subagent shares the root session id).
        let thread_wins = resolver
            .resolve(
                &env(&[("CODEX_THREAD_ID", "thread-b"), ("CODEX_SESSION_ID", "thread-a")]),
                Some(CWD),
                BETWEEN,
                None,
                true,
                true,
            )
            .unwrap();
        assert_eq!(thread_wins.id, "thread-b");
        assert!(!thread_wins.estimated);

        // Both chats already existed and the transcript names the command in A only.
        let by_command = resolver
            .resolve(&none, Some(CWD), AFTER_BOTH, Some("next dev --port 3000"), true, true)
            .unwrap();
        assert_eq!(by_command.id, "thread-a");
        assert!(!by_command.estimated);

        // Nothing distinguishes the two chats. Keep a title, but flag the guess.
        let guessed = resolver.resolve(&none, Some(CWD), AFTER_BOTH, Some("npm run dev"), true, true).unwrap();
        assert!(guessed.estimated);
        assert_eq!(guessed.id, "thread-b", "the later chat is only a best guess");
        assert_eq!(guessed.kind, "Codex");

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn claude_session_id_is_exact_and_not_replaced_by_codex() {
        let (root, mut resolver) = fixture("claude");
        let session = resolver
            .resolve(
                &env(&[("CLAUDE_CODE_SESSION_ID", "claude-session-xyz")]),
                Some(CWD),
                AFTER_BOTH,
                None,
                true,
                true,
            )
            .unwrap();
        assert_eq!(session.kind, "Claude Code");
        assert_eq!(session.id, "claude-session-xyz");
        assert!(!session.estimated);
        fs::remove_dir_all(&root).ok();
    }
}
