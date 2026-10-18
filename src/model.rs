use std::collections::HashMap;

use serde::Serialize;

use crate::agents::AgentSession;
use crate::format;
use crate::procfs::Args;
use crate::project::ProjectInfo;

pub const LEAK_BYTES: u64 = 500 * 1024 * 1024;

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Sample {
    pub time: f64,
    pub memory: u64,
    pub cpu: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ServerProcess {
    pub pid: i32,
    pub name: String,
    pub depth: usize,
    pub memory: u64,
    pub cpu: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Server {
    /// Stable identity across scans: port, root pid and its start time.
    pub key: String,
    pub port: u16,
    pub pid: i32,
    pub root_pid: i32,
    pub process_name: String,
    pub addresses: Vec<String>,
    pub cwd: Option<String>,
    pub cwd_exists: bool,
    pub command: Option<String>,
    #[serde(skip)]
    pub launch: Option<Args>,
    pub launch_directory: Option<String>,
    pub started_at: f64,
    pub project: ProjectInfo,
    pub agent: Option<AgentSession>,
    pub processes: Vec<ServerProcess>,
    /// pid -> start time, used to make sure we only ever signal the processes we scanned.
    #[serde(skip)]
    pub process_starts: HashMap<i32, f64>,
    pub memory: u64,
    pub cpu: f64,
    pub connections: u32,
    pub history: Vec<Sample>,
    pub last_active: f64,
    pub is_protected: bool,
}

impl Server {
    pub fn url(&self) -> String {
        format!("http://localhost:{}", self.port)
    }

    pub fn display_name(&self) -> String {
        if !self.project.name.is_empty() && self.project.name != "Unknown" {
            return self.project.name.clone();
        }
        self.command.clone().unwrap_or_else(|| self.process_name.clone())
    }

    pub fn uptime(&self, now: f64) -> f64 {
        (now - self.started_at).max(0.0)
    }

    pub fn idle_for(&self, now: f64) -> f64 {
        (now - self.last_active).max(0.0)
    }

    pub fn memory_growth(&self) -> u64 {
        match self.history.first() {
            Some(first) if self.history.len() >= 2 => self.memory.saturating_sub(first.memory),
            _ => 0,
        }
    }

    pub fn is_leaking(&self) -> bool {
        self.memory_growth() > LEAK_BYTES
    }

    pub fn needs_attention(&self, threshold: u64) -> bool {
        self.memory >= threshold || self.is_leaking()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CleanupReason {
    WorktreeDeleted,
    Idle(String),
    LongRunning(String),
    Leaking(String),
}

impl CleanupReason {
    pub fn is_leak(&self) -> bool {
        matches!(self, Self::Leaking(_))
    }

    pub fn label(&self) -> String {
        match self {
            Self::WorktreeDeleted => "Worktree deleted".into(),
            Self::Idle(d) => format!("Idle {d}"),
            Self::LongRunning(d) => format!("Running {d}"),
            Self::Leaking(d) => format!("+{d} in 10m"),
        }
    }
}

pub fn cleanup_reason(s: &Server, prefs: &crate::prefs::Prefs, now: f64) -> Option<CleanupReason> {
    use crate::prefs::CleanupMode;
    if s.is_protected || prefs.cleanup_mode == CleanupMode::Off {
        return None;
    }
    if !s.cwd_exists {
        return prefs.cleanup_deleted_worktree.then_some(CleanupReason::WorktreeDeleted);
    }
    if s.idle_for(now) >= prefs.idle_threshold_secs() {
        return Some(CleanupReason::Idle(format::duration(s.idle_for(now))));
    }
    if s.uptime(now) >= prefs.running_threshold_secs() {
        return Some(CleanupReason::LongRunning(format::duration(s.uptime(now))));
    }
    if s.is_leaking() {
        return Some(CleanupReason::Leaking(format::bytes(s.memory_growth())));
    }
    None
}
