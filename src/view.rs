//! The view model every frontend renders: already formatted, already decided.

use std::collections::HashMap;

use serde::Serialize;
use wtp_core::format;
use wtp_core::github::{GitHubInfo, PullRequest, Preview};
use wtp_core::model::{self, Server};
use wtp_core::prefs::{CleanupMode, Prefs};
use wtp_core::procfs::SystemMemory;

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct State {
    pub count: usize,
    pub attention: bool,
    pub summary: Summary,
    pub servers: Vec<ServerView>,
    pub retired: Vec<RetiredView>,
    pub cleanup: Option<CleanupView>,
    pub prefs: PrefsView,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct Summary {
    pub servers_memory: String,
    pub other_memory: String,
    pub free_memory: String,
    pub cpu: String,
    pub segments: Vec<Segment>,
    pub other_fraction: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Segment {
    pub port: u16,
    pub name: String,
    pub memory: String,
    pub percent: String,
    pub color: String,
    pub fraction: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ServerView {
    pub key: String,
    pub port: u16,
    pub name: String,
    pub color: String,
    pub memory: String,
    pub memory_bytes: u64,
    pub cpu: String,
    pub attention: bool,
    pub leaking: bool,
    pub protected: bool,
    pub snoozed: bool,
    pub url: String,
    /// One-line context under the name: agent session, branch or problem, plus uptime.
    pub subtitle: String,
    pub framework: Option<String>,
    pub branch: Option<String>,
    pub agent: Option<String>,
    /// True when `agent` was inferred from the working folder rather than identified.
    pub session_estimated: bool,
    pub uptime: String,
    pub uptime_seconds: u64,
    pub command: Option<String>,
    pub directory: Option<String>,
    pub growth: Option<String>,
    pub cleanup: Option<String>,
    pub connections: u32,
    pub processes: Vec<ProcessView>,
    /// Memory over the last 10 minutes, scaled to 0..1.
    pub spark: Vec<f64>,
    pub can_restart: bool,
    pub pull_request: Option<PullRequest>,
    pub preview: Option<Preview>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProcessView {
    pub name: String,
    pub depth: usize,
    pub memory: String,
    pub cpu: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RetiredView {
    pub port: u16,
    pub name: String,
    pub state: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct CleanupView {
    pub count: usize,
    pub label: String,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct PrefsView {
    pub threshold_gb: f64,
    pub cleanup_mode: String,
    pub leak_warnings: bool,
    pub link_claude: bool,
    pub link_codex: bool,
    pub show_branches: bool,
    pub github_pull_requests: bool,
    pub vercel_previews: bool,
    pub launch_at_login: bool,
}

pub struct Inputs<'a> {
    pub now: f64,
    pub prefs: &'a Prefs,
    pub servers: &'a [Server],
    pub colors: &'a HashMap<u16, usize>,
    pub memory: &'a SystemMemory,
    pub retired: Vec<RetiredView>,
    pub snoozed: &'a HashMap<String, f64>,
    pub github: &'a HashMap<String, (GitHubInfo, f64)>,
    pub launch_at_login: bool,
}

const SPARK_POINTS: usize = 30;

pub fn build(i: Inputs) -> State {
    let threshold = i.prefs.threshold_bytes();
    let retired_ports: Vec<u16> = i.retired.iter().map(|r| r.port).collect();
    let active: Vec<&Server> = i.servers.iter().filter(|s| !retired_ports.contains(&s.port)).collect();

    let mut unique: HashMap<i32, (u64, f64)> = HashMap::new();
    for s in &active {
        for p in &s.processes {
            unique.insert(p.pid, (p.memory, p.cpu));
        }
    }
    let server_mem: u64 = unique.values().map(|v| v.0).sum();
    let server_cpu: f64 = unique.values().map(|v| v.1).sum();
    let total = i.memory.total.max(1);
    let used = i.memory.total.saturating_sub(i.memory.available);
    let other = used.saturating_sub(server_mem);

    let color = |port: u16| format::port_color(i.colors.get(&port).copied().unwrap_or(0)).to_string();
    let servers: Vec<ServerView> = active.iter().map(|s| server_view(s, &i, threshold, color(s.port))).collect();
    let attention = servers.iter().any(|s| s.attention);

    let cleanable = active
        .iter()
        .filter(|s| model::cleanup_reason(s, i.prefs, i.now).is_some_and(|r| !r.is_leak()))
        .count();

    State {
        count: servers.len(),
        attention,
        summary: Summary {
            servers_memory: format::total(server_mem),
            other_memory: format::total(other),
            free_memory: format::total(i.memory.available),
            cpu: format::percent(server_cpu / wtp_core::procfs::cpu_count()),
            segments: active
                .iter()
                .map(|s| {
                    let fraction = s.memory as f64 / total as f64;
                    Segment {
                        port: s.port,
                        name: s.display_name(),
                        memory: format::bytes(s.memory),
                        percent: format::percent(fraction * 100.0),
                        color: color(s.port),
                        fraction,
                    }
                })
                .collect(),
            other_fraction: other as f64 / total as f64,
        },
        servers,
        retired: i.retired,
        cleanup: (cleanable > 0).then(|| CleanupView {
            count: cleanable,
            label: format!("Clean up {cleanable} server{}", if cleanable == 1 { "" } else { "s" }),
        }),
        prefs: PrefsView {
            threshold_gb: i.prefs.threshold_gb,
            cleanup_mode: match i.prefs.cleanup_mode {
                CleanupMode::Off => "off",
                CleanupMode::Ask => "ask",
                CleanupMode::Automatic => "automatic",
            }
            .into(),
            leak_warnings: i.prefs.leak_warnings,
            link_claude: i.prefs.link_claude,
            link_codex: i.prefs.link_codex,
            show_branches: i.prefs.show_branches,
            github_pull_requests: i.prefs.github_pull_requests,
            vercel_previews: i.prefs.vercel_previews,
            launch_at_login: i.launch_at_login,
        },
    }
}

/// Prefix a session title when the link is only a folder guess. Idempotent.
pub(crate) fn mark_estimated(text: &str, estimated: bool) -> String {
    if estimated && !text.starts_with('~') { format!("~{text}") } else { text.to_string() }
}

fn server_view(s: &Server, i: &Inputs, threshold: u64, color: String) -> ServerView {
    let uptime = format::duration(s.uptime(i.now));
    let estimated = s.agent.as_ref().is_some_and(|a| a.estimated);
    let agent = s.agent.as_ref().map(|a| {
        let title = a.title.clone().unwrap_or_else(|| a.kind.to_string());
        mark_estimated(&title, a.estimated)
    });
    let context = agent
        .clone()
        .or_else(|| s.project.branch.clone())
        .or_else(|| (!s.cwd_exists).then(|| "Worktree deleted".into()));
    let subtitle = match context {
        Some(c) => format!("{c} · up {uptime}"),
        None => format!("up {uptime}"),
    };
    let growth = s.memory_growth();
    let github = match (&s.project.root, &s.project.branch) {
        (Some(root), Some(branch)) => i.github.get(&format!("{root}|{branch}")).map(|g| g.0.clone()),
        _ => None,
    }
    .unwrap_or_default();

    ServerView {
        key: s.key.clone(),
        port: s.port,
        name: s.display_name(),
        color: if s.needs_attention(threshold) { format::AMBER.into() } else { color },
        memory: format::bytes(s.memory),
        memory_bytes: s.memory,
        cpu: format::percent(s.cpu),
        attention: s.needs_attention(threshold),
        leaking: s.is_leaking(),
        protected: s.is_protected,
        snoozed: i.snoozed.get(&s.key).is_some_and(|until| *until > i.now),
        url: s.url(),
        subtitle,
        framework: s.project.framework.clone(),
        branch: s.project.branch.clone(),
        agent: s.agent.as_ref().map(|a| match &a.title {
            Some(t) => format!("{}: {}", a.kind, mark_estimated(t, a.estimated)),
            None => mark_estimated(a.kind, a.estimated),
        }),
        session_estimated: estimated,
        uptime_seconds: s.uptime(i.now) as u64,
        uptime,
        command: s.command.clone(),
        directory: s.cwd.clone(),
        growth: (growth >= 1024 * 1024).then(|| format!("+{} in 10 min", format::bytes(growth))),
        cleanup: model::cleanup_reason(s, i.prefs, i.now).map(|r| r.label()),
        connections: s.connections,
        processes: s
            .processes
            .iter()
            .map(|p| ProcessView {
                name: p.name.clone(),
                depth: p.depth,
                memory: format::bytes(p.memory),
                cpu: format::percent(p.cpu),
            })
            .collect(),
        spark: spark(s),
        can_restart: wtp_core::control::can_restart(s),
        pull_request: github.pull_request,
        preview: github.preview,
    }
}

/// Bucket-average the memory history and scale it to 0..1. A flat line stays flat
/// instead of amplifying noise, by never using a range below 10% of the peak.
fn spark(s: &Server) -> Vec<f64> {
    let h = &s.history;
    if h.len() < 2 {
        return Vec::new();
    }
    let buckets = SPARK_POINTS.min(h.len());
    let values: Vec<f64> = (0..buckets)
        .map(|b| {
            let chunk = &h[b * h.len() / buckets..((b + 1) * h.len() / buckets).max(b * h.len() / buckets + 1)];
            chunk.iter().map(|x| x.memory as f64).sum::<f64>() / chunk.len() as f64
        })
        .collect();
    let max = values.iter().cloned().fold(0.0, f64::max);
    let min = values.iter().cloned().fold(f64::MAX, f64::min);
    let range = (max - min).max(max * 0.1).max(1.0);
    let floor = max - range;
    values.iter().map(|v| ((v - floor) / range * 1000.0).round() / 1000.0).collect()
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;
    use wtp_core::agents::AgentSession;
    use wtp_core::model::Server;
    use wtp_core::prefs::Prefs;
    use wtp_core::procfs::SystemMemory;
    use wtp_core::project::ProjectInfo;

    fn view_of(agent: AgentSession) -> ServerView {
        let prefs = Prefs::default();
        let memory = SystemMemory { total: 8 << 30, available: 4 << 30 };
        let servers = vec![Server {
            key: "3000-1-1".into(),
            port: 3000,
            pid: 1,
            root_pid: 1,
            process_name: "node".into(),
            addresses: vec![],
            cwd: Some("/workspace/app".into()),
            cwd_exists: true,
            command: Some("next dev".into()),
            launch: None,
            launch_directory: None,
            started_at: 1_000_000.0,
            project: ProjectInfo { name: "docs-site".into(), ..Default::default() },
            agent: Some(agent),
            processes: vec![],
            process_starts: HashMap::new(),
            memory: 50 * 1024 * 1024,
            cpu: 0.5,
            connections: 0,
            history: vec![],
            last_active: 1_000_060.0,
            is_protected: false,
        }];
        let colors = HashMap::new();
        let snoozed = HashMap::new();
        let github = HashMap::new();
        build(Inputs {
            now: 1_000_060.0,
            prefs: &prefs,
            servers: &servers,
            colors: &colors,
            memory: &memory,
            retired: vec![],
            snoozed: &snoozed,
            github: &github,
            launch_at_login: false,
        })
        .servers
        .into_iter()
        .next()
        .unwrap()
    }

    #[test]
    fn estimated_codex_session_is_marked_in_the_view() {
        let exact = view_of(AgentSession {
            kind: "Codex",
            id: "thread-a".into(),
            title: Some("Fix OAuth callback".into()),
            estimated: false,
        });
        assert!(!exact.session_estimated);
        assert_eq!(exact.subtitle, "Fix OAuth callback · up 1m");
        assert_eq!(exact.agent.as_deref(), Some("Codex: Fix OAuth callback"));

        let guessed = view_of(AgentSession {
            kind: "Codex",
            id: "thread-b".into(),
            title: Some("Migrate docs to Astro 5".into()),
            estimated: true,
        });
        assert!(guessed.session_estimated);
        assert_eq!(guessed.subtitle, "~Migrate docs to Astro 5 · up 1m");
        assert_eq!(guessed.agent.as_deref(), Some("Codex: ~Migrate docs to Astro 5"));
        assert!(serde_json::to_string(&guessed).unwrap().contains("\"session_estimated\":true"));
        assert_eq!(mark_estimated(&guessed.subtitle, true), guessed.subtitle);
    }
}
