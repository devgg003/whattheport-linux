//! Resource-alert copy and repeat suppression.
//!
//! Live alerts measure resident memory, not network traffic. `Server::memory`
//! is the sum of RSS (`/proc/<pid>/statm` resident pages) for the listening
//! process, the runner ancestors above it (`npm`, `pnpm`, `node`, …), and
//! their descendants. A Docker container uses that sum, or cgroup
//! `memory.current` when the cgroup figure is higher. CPU is the same
//! per-server sum already shown in the menu (a process can exceed 100% when
//! it occupies more than one core). The share of RAM is that RSS total
//! divided by `MemTotal`.
//!
//! Growth is the current total minus the oldest sample still inside the
//! 10-minute history (`scan::HISTORY_SECS`). The sample window is often
//! shorter than 10 minutes — a server that has only been watched for 45
//! seconds must not be described as a 10-minute change. Nothing in this path
//! counts bytes sent or received; `Server::connections` is a connection count
//! and is not an alert.

use crate::agents::AgentSession;
use crate::format;
use crate::model::{Sample, Server};

/// Smallest extra usage that counts as a new step once a server is already alerting.
pub const ESCALATION_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResourceKind {
    /// Resident memory of the server process tree.
    Memory,
    /// Bytes transferred. No live alert uses this; the copy is here so a
    /// network reading cannot be presented as memory.
    Network,
}

impl ResourceKind {
    pub fn total_label(self) -> &'static str {
        match self {
            Self::Memory => "Total memory",
            Self::Network => "Total network",
        }
    }

    pub fn change_label(self) -> &'static str {
        match self {
            Self::Memory => "Current change",
            Self::Network => "Current rate",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ResourceReading {
    pub kind: ResourceKind,
    pub total: u64,
    /// Signed change over the sample window. Negative means the total fell.
    pub delta: i64,
    /// Seconds between the oldest and newest sample. Only meaningful when `has_window`.
    pub window_secs: f64,
    pub has_window: bool,
    pub cpu_percent: f64,
    /// `total / MemTotal * 100` for a memory reading. Callers set this for network too
    /// so the resources line can still show the server's share of RAM.
    pub ram_percent: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertSubject {
    pub port: u16,
    pub name: String,
    pub command: Option<String>,
    pub session: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertText {
    /// Port, project or command, and the linked session when there is one.
    pub summary: String,
    pub body: String,
    /// "Total memory: 1.4 GB"
    pub total: String,
    /// "+120 MB in the last 5 min", or a note that a second sample does not exist yet.
    pub change: String,
    /// "Current change: +120 MB in the last 5 min"
    pub change_labeled: String,
    /// "CPU 35% · 9.0% of RAM"
    pub resources: String,
}

/// Last totals we notified for. A later alert requires a meaningful step up from these.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AlertLevel {
    pub total: u64,
    pub growth: u64,
}

pub fn mark_estimated(text: &str, estimated: bool) -> String {
    if estimated && !text.starts_with('~') {
        format!("~{text}")
    } else {
        text.to_string()
    }
}

pub fn session_label(agent: &AgentSession) -> String {
    match &agent.title {
        Some(title) => format!("{}: {}", agent.kind, mark_estimated(title, agent.estimated)),
        None => mark_estimated(agent.kind, agent.estimated),
    }
}

pub fn subject_from_server(server: &Server) -> AlertSubject {
    let name = server.display_name();
    let command = server.command.clone().filter(|cmd| cmd != &name);
    AlertSubject {
        port: server.port,
        name,
        command,
        session: server.agent.as_ref().map(session_label),
    }
}

/// Change in resident memory across the retained history.
///
/// Returns `(delta, window_secs, has_window)`. `has_window` is false until a
/// second sample exists, so the copy does not invent a 10-minute window.
pub fn memory_delta(memory: u64, history: &[Sample]) -> (i64, f64, bool) {
    if history.len() < 2 {
        return (0, 0.0, false);
    }
    let first = &history[0];
    let last = &history[history.len() - 1];
    let window = (last.time - first.time).max(0.0);
    let delta = memory as i64 - first.memory as i64;
    (delta, window, true)
}

pub fn memory_reading(server: &Server, system_total: u64) -> ResourceReading {
    let (delta, window_secs, has_window) = memory_delta(server.memory, &server.history);
    ResourceReading {
        kind: ResourceKind::Memory,
        total: server.memory,
        delta,
        window_secs,
        has_window,
        cpu_percent: server.cpu,
        ram_percent: ram_share(server.memory, system_total),
    }
}

pub fn ram_share(memory: u64, system_total: u64) -> f64 {
    if system_total == 0 {
        0.0
    } else {
        memory as f64 / system_total as f64 * 100.0
    }
}

pub fn window_seconds(secs: f64) -> u64 {
    if !secs.is_finite() || secs <= 0.0 {
        0
    } else {
        secs.round() as u64
    }
}

/// "in the last 5 min" / "in the last 45 sec". The number is the real span.
pub fn window_phrase(secs: f64) -> String {
    if !secs.is_finite() || secs < 1.0 {
        return "in the last few seconds".into();
    }
    if secs < 60.0 {
        return format!("in the last {} sec", secs.round() as u64);
    }
    let minutes = (secs / 60.0).round().max(1.0) as u64;
    format!("in the last {minutes} min")
}

pub fn signed_total(delta: i64) -> String {
    let body = format::total(delta.unsigned_abs());
    if delta < 0 { format!("-{body}") } else { format!("+{body}") }
}

pub fn change_phrase(delta: i64, window_secs: f64, has_window: bool) -> String {
    if !has_window {
        return "not enough history yet".into();
    }
    format!("{} {}", signed_total(delta), window_phrase(window_secs))
}

pub fn resources_label(cpu_percent: f64, ram_percent: f64) -> String {
    format!("CPU {} · {} of RAM", format::percent(cpu_percent), format::percent(ram_percent))
}

pub fn format_alert(subject: &AlertSubject, reading: &ResourceReading) -> AlertText {
    let summary = identity_line(subject);
    let total = format!("{}: {}", reading.kind.total_label(), format::total(reading.total));
    let change = change_phrase(reading.delta, reading.window_secs, reading.has_window);
    let change_labeled = format!("{}: {change}", reading.kind.change_label());
    let resources = resources_label(reading.cpu_percent, reading.ram_percent);
    let body = format!("{total}\n{change_labeled}\n{resources}");
    AlertText { summary, body, total, change, change_labeled, resources }
}

pub fn format_server_alert(server: &Server, system_total: u64) -> AlertText {
    format_alert(&subject_from_server(server), &memory_reading(server, system_total))
}

fn identity_line(subject: &AlertSubject) -> String {
    let mut parts = vec![format!("Port {}", subject.port), subject.name.clone()];
    if let Some(command) = &subject.command {
        parts.push(command.clone());
    }
    if let Some(session) = &subject.session {
        parts.push(session.clone());
    }
    parts.join(" · ")
}

/// True when `current` is far enough above `previous` to deserve a new alert.
///
/// The step is the larger of 256 MB and 25% of the previous value, so a server
/// that ticks up by a few megabytes does not escalate, and a multi-gigabyte
/// server does not escalate on a fixed 256 MB nudge.
pub fn meaningfully_higher(previous: u64, current: u64) -> bool {
    if current <= previous {
        return false;
    }
    let step = (previous / 4).max(ESCALATION_BYTES);
    current - previous >= step
}

/// Whether this scan should emit a resource notification.
///
/// `cooldown_until` is the existing snooze deadline (`f64::INFINITY` when the
/// user mutes the server). A repeat waits out that deadline, and still stays
/// quiet unless total memory or growth has crossed a higher step. The first
/// alert for a server (no previous level, and not inside a cooldown) is sent.
pub fn should_alert(
    previous: Option<AlertLevel>,
    cooldown_until: Option<f64>,
    now: f64,
    total: u64,
    growth: u64,
) -> bool {
    if cooldown_until.is_some_and(|until| now < until) {
        return false;
    }
    let Some(previous) = previous else {
        return true;
    };
    meaningfully_higher(previous.total, total) || meaningfully_higher(previous.growth, growth)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Sample;
    use crate::project::ProjectInfo;

    fn mb(n: u64) -> u64 {
        n * 1024 * 1024
    }

    fn gb(n: f64) -> u64 {
        (n * 1024.0 * 1024.0 * 1024.0) as u64
    }

    fn subject() -> AlertSubject {
        AlertSubject {
            port: 5173,
            name: "studio-vite".into(),
            command: Some("vite --port 5173".into()),
            session: Some("Claude Code: Fix OAuth callback".into()),
        }
    }

    fn reading(kind: ResourceKind, total: u64, delta: i64, window_secs: f64, has_window: bool) -> ResourceReading {
        ResourceReading {
            kind,
            total,
            delta,
            window_secs,
            has_window,
            cpu_percent: 35.0,
            ram_percent: 9.0,
        }
    }

    #[test]
    fn memory_alert_labels_total_change_and_resources() {
        let text = format_alert(&subject(), &reading(ResourceKind::Memory, gb(1.4), mb(120) as i64, 5.0 * 60.0, true));
        assert_eq!(text.total, "Total memory: 1.4 GB");
        assert_eq!(text.change, "+120 MB in the last 5 min");
        assert_eq!(text.change_labeled, "Current change: +120 MB in the last 5 min");
        assert_eq!(text.resources, "CPU 35% · 9.0% of RAM");
        assert_eq!(
            text.summary,
            "Port 5173 · studio-vite · vite --port 5173 · Claude Code: Fix OAuth callback"
        );
        assert_eq!(
            text.body,
            "Total memory: 1.4 GB\nCurrent change: +120 MB in the last 5 min\nCPU 35% · 9.0% of RAM"
        );
        assert!(!text.body.contains("in 10 minutes"), "the window is the real span, not a fixed 10 minutes");
    }

    #[test]
    fn network_alert_is_labeled_as_network_with_a_rate() {
        let text = format_alert(&subject(), &reading(ResourceKind::Network, gb(1.4), mb(120) as i64, 5.0 * 60.0, true));
        assert_eq!(text.total, "Total network: 1.4 GB");
        assert_eq!(text.change_labeled, "Current rate: +120 MB in the last 5 min");
        assert!(text.body.starts_with("Total network:"));
        assert!(!text.body.contains("Total memory"));
        assert!(text.resources.contains("CPU 35%"));
        assert!(text.resources.contains("of RAM"));
    }

    #[test]
    fn short_window_and_shrinkage_are_explicit() {
        assert_eq!(change_phrase(mb(120) as i64, 45.0, true), "+120 MB in the last 45 sec");
        assert_eq!(change_phrase(-(mb(40) as i64), 600.0, true), "-40 MB in the last 10 min");
        assert_eq!(change_phrase(0, 600.0, true), "+0 MB in the last 10 min");
        assert_eq!(change_phrase(mb(120) as i64, 0.0, false), "not enough history yet");
        assert_eq!(window_phrase(0.2), "in the last few seconds");
    }

    #[test]
    fn memory_delta_uses_the_real_history_span() {
        let history = vec![
            Sample { time: 1_000.0, memory: mb(80), cpu: 0.0 },
            Sample { time: 1_045.0, memory: mb(200), cpu: 1.0 },
        ];
        assert_eq!(memory_delta(mb(200), &history), (mb(120) as i64, 45.0, true));
        assert_eq!(memory_delta(mb(200), &history[..1]), (0, 0.0, false));
        let text = format_alert(
            &AlertSubject { port: 3000, name: "leak-lab".into(), command: None, session: None },
            &reading(ResourceKind::Memory, mb(200), mb(120) as i64, 45.0, true),
        );
        assert_eq!(text.summary, "Port 3000 · leak-lab");
        assert!(text.change.contains("in the last 45 sec"));
        assert!(!text.change.contains("10 min"));
    }

    #[test]
    fn server_alert_names_the_session_and_measures_rss_growth() {
        let server = Server {
            key: "5173-1-1".into(),
            port: 5173,
            pid: 1,
            root_pid: 1,
            process_name: "node".into(),
            addresses: vec![],
            cwd: Some("/workspace/app".into()),
            cwd_exists: true,
            command: Some("vite --port 5173".into()),
            launch: None,
            launch_directory: None,
            started_at: 1_000.0,
            project: ProjectInfo { name: "studio-vite".into(), ..Default::default() },
            agent: Some(AgentSession {
                kind: "Codex",
                id: "thread".into(),
                title: Some("Migrate docs".into()),
                estimated: true,
            }),
            processes: vec![],
            process_starts: Default::default(),
            memory: mb(640),
            cpu: 35.0,
            connections: 0,
            history: vec![
                Sample { time: 1_000.0, memory: mb(520), cpu: 0.0 },
                Sample { time: 1_300.0, memory: mb(640), cpu: 35.0 },
            ],
            last_active: 1_300.0,
            is_protected: false,
        };
        let text = format_server_alert(&server, 16 * 1024 * 1024 * 1024);
        assert_eq!(text.summary, "Port 5173 · studio-vite · vite --port 5173 · Codex: ~Migrate docs");
        assert_eq!(text.total, "Total memory: 640 MB");
        assert_eq!(text.change, "+120 MB in the last 5 min");
        assert!(text.resources.starts_with("CPU 35%"));
        assert!(text.resources.contains("of RAM"));
    }

    #[test]
    fn escalation_steps_are_at_least_256_mb_or_25_percent() {
        assert!(!meaningfully_higher(mb(500), mb(500) + mb(256) - 1));
        assert!(meaningfully_higher(mb(500), mb(500) + mb(256)));
        let two_gb = gb(2.0);
        assert!(!meaningfully_higher(two_gb, two_gb + mb(512) - 1));
        assert!(meaningfully_higher(two_gb, two_gb + mb(512)));
        assert!(!meaningfully_higher(mb(400), mb(400)));
        assert!(!meaningfully_higher(mb(400), mb(100)));
    }

    #[test]
    fn repeats_wait_for_cooldown_and_a_higher_step() {
        let level = AlertLevel { total: mb(600), growth: mb(520) };
        let snooze_until = 1_000.0 + 60.0 * 60.0;

        assert!(should_alert(None, None, 1_000.0, mb(600), mb(520)), "first alert");
        assert!(
            !should_alert(None, Some(f64::INFINITY), 1_000.0, mb(4_000), mb(3_000)),
            "mute blocks even a large jump"
        );
        assert!(
            !should_alert(Some(level), Some(snooze_until), 1_500.0, mb(3_000), mb(2_000)),
            "a meaningful jump inside the snooze stays quiet"
        );
        assert!(
            !should_alert(Some(level), Some(snooze_until), snooze_until + 1.0, mb(610), mb(530)),
            "after the snooze, a few extra megabytes are still the same alert"
        );
        assert!(
            should_alert(Some(level), Some(snooze_until), snooze_until + 1.0, mb(600) + mb(256), mb(520)),
            "after the snooze, crossing the next step alerts again"
        );
        assert!(
            !should_alert(Some(level), Some(0.0), 2.0, mb(610), mb(530)),
            "a zero snooze still does not repeat the same step on the next scan"
        );
        assert!(
            should_alert(Some(level), Some(0.0), 2.0, mb(600), mb(520) + mb(256)),
            "a zero snooze still escalates when growth crosses the next step"
        );
    }
}
