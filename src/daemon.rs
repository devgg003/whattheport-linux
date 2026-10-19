//! The background service: scans, decides, publishes the view model on D-Bus,
//! and carries out commands from the panel menu, tray, notifications and CLI.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::error::Error;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::signal::unix::{SignalKind, signal};
use tokio::sync::{Notify, mpsc};
use zbus::object_server::SignalEmitter;
use wtp_core::control;
use wtp_core::format;
use wtp_core::github::{self, GitHubInfo};
use wtp_core::model::{self, Server};
use wtp_core::prefs::{CleanupMode, Prefs, TrayMode};
use wtp_core::procfs::{self, SystemMemory};
use wtp_core::scan::ScanEngine;

use crate::notify::{Notice, Notifier};
use crate::tray;
use crate::view::{self, RetiredView, State};

pub const BUS_NAME: &str = "dev.whattheport.WhatThePort";
pub const OBJECT_PATH: &str = "/dev/whattheport/Daemon";
pub const INTERFACE: &str = "dev.whattheport.Daemon1";

const GITHUB_MAX_AGE: f64 = 120.0;

#[derive(Debug, Clone, Copy)]
pub enum Link {
    Local,
    PullRequest,
    Preview,
}

#[derive(Debug, Clone)]
pub enum Command {
    Open(u16, Link),
    Stop(u16),
    Restart(u16),
    Snooze(u16),
    CleanUp,
    SetPref(String, serde_json::Value),
    Quit,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Phase {
    Stopping,
    Stopped,
    Restarting,
}

struct Retired {
    name: String,
    phase: Phase,
    since: f64,
}

struct StopJob {
    port: u16,
    targets: HashMap<i32, f64>,
    grace: f64,
    restart: Option<Server>,
}

#[derive(Default)]
struct ScanOutput {
    notices: Vec<Notice>,
    auto_stop: Vec<u16>,
    lookups: Vec<(String, String)>,
}

pub struct Core {
    prefs: Prefs,
    engine: ScanEngine,
    servers: Vec<Server>,
    colors: HashMap<u16, usize>,
    memory: SystemMemory,
    retired: BTreeMap<u16, Retired>,
    snoozed: HashMap<String, f64>,
    handled_cleanups: HashSet<String>,
    github: HashMap<String, (GitHubInfo, f64)>,
    state: State,
    json: String,
}

impl Core {
    fn new() -> Self {
        Self {
            prefs: Prefs::load(),
            engine: ScanEngine::default(),
            servers: Vec::new(),
            colors: HashMap::new(),
            memory: SystemMemory { total: 0, available: 0 },
            retired: BTreeMap::new(),
            snoozed: HashMap::new(),
            handled_cleanups: HashSet::new(),
            github: HashMap::new(),
            state: State::default(),
            json: "{}".into(),
        }
    }

    fn server(&self, port: u16) -> Option<&Server> {
        self.servers.iter().find(|s| s.port == port)
    }

    fn scan(&mut self) -> ScanOutput {
        let now = wtp_core::now();
        self.servers = self.engine.scan(&self.prefs);
        self.memory = procfs::system_memory();
        for s in &self.servers {
            let next = self.colors.len();
            self.colors.entry(s.port).or_insert(next);
        }

        let live: HashSet<u16> = self.servers.iter().map(|s| s.port).collect();
        self.retired.retain(|port, r| match r.phase {
            Phase::Stopping => true,
            Phase::Stopped => !live.contains(port) && now - r.since < 4.0,
            Phase::Restarting => !live.contains(port) && now - r.since < 30.0,
        });

        let mut out = ScanOutput::default();
        self.evaluate_alerts(now, &mut out);
        self.evaluate_cleanup(now, &mut out);

        if self.prefs.github_pull_requests || self.prefs.vercel_previews {
            for s in &self.servers {
                if let (Some(root), Some(branch)) = (&s.project.root, &s.project.branch) {
                    let key = format!("{root}|{branch}");
                    let stale = self.github.get(&key).is_none_or(|(_, at)| now - at > GITHUB_MAX_AGE);
                    if stale {
                        let old = self.github.remove(&key).map(|g| g.0).unwrap_or_default();
                        self.github.insert(key, (old, now));
                        out.lookups.push((root.clone(), branch.clone()));
                    }
                }
            }
        }
        out
    }

    fn evaluate_alerts(&mut self, now: f64, out: &mut ScanOutput) {
        let keys: HashSet<&str> = self.servers.iter().map(|s| s.key.as_str()).collect();
        self.snoozed.retain(|k, _| keys.contains(k.as_str()));
        if !self.prefs.leak_warnings {
            return;
        }
        let threshold = self.prefs.threshold_bytes();
        for s in self.servers.iter().filter(|s| s.needs_attention(threshold)) {
            if self.snoozed.get(&s.key).is_some_and(|until| now < *until) {
                continue;
            }
            self.snoozed.insert(s.key.clone(), now + self.prefs.snooze_minutes as f64 * 60.0);
            let body = if s.is_leaking() {
                format!("{} grew by {} in 10 minutes and is now using {}.", s.display_name(), format::bytes(s.memory_growth()), format::bytes(s.memory))
            } else {
                format!("{} is using {} (alert threshold {}).", s.display_name(), format::bytes(s.memory), format::total(threshold))
            };
            let mut actions = vec![("snooze", Command::Snooze(s.port))];
            if !s.is_protected {
                actions.insert(0, ("stop", Command::Stop(s.port)));
            }
            out.notices.push(Notice { summary: format!("Port {} needs attention", s.port), body, critical: true, actions });
        }
    }

    fn evaluate_cleanup(&mut self, now: f64, out: &mut ScanOutput) {
        if self.prefs.cleanup_mode == CleanupMode::Off {
            self.handled_cleanups.clear();
            return;
        }
        let candidates: Vec<(&Server, String)> = self
            .servers
            .iter()
            .filter(|s| !self.retired.contains_key(&s.port))
            .filter_map(|s| model::cleanup_reason(s, &self.prefs, now).filter(|r| !r.is_leak()).map(|r| (s, r.label())))
            .collect();
        let current: HashSet<String> = candidates.iter().map(|(s, _)| s.key.clone()).collect();
        let fresh: Vec<&(&Server, String)> = candidates.iter().filter(|(s, _)| !self.handled_cleanups.contains(&s.key)).collect();
        self.handled_cleanups.retain(|k| current.contains(k));
        if fresh.is_empty() {
            return;
        }
        self.handled_cleanups.extend(fresh.iter().map(|(s, _)| s.key.clone()));
        let names = fresh.iter().map(|(s, why)| format!(":{} ({why})", s.port)).collect::<Vec<_>>().join(", ");
        match self.prefs.cleanup_mode {
            CleanupMode::Automatic => {
                out.auto_stop.extend(fresh.iter().map(|(s, _)| s.port));
                if self.prefs.cleanup_notify {
                    out.notices.push(Notice { summary: "Stopped idle servers".into(), body: names, critical: false, actions: vec![] });
                }
            }
            CleanupMode::Ask if self.prefs.cleanup_notify => out.notices.push(Notice {
                summary: "Servers ready to clean up".into(),
                body: names,
                critical: false,
                actions: vec![("cleanup", Command::CleanUp)],
            }),
            _ => {}
        }
    }

    /// Rebuild the view model; returns true if anything visible changed.
    fn rebuild(&mut self) -> bool {
        let state = view::build(view::Inputs {
            now: wtp_core::now(),
            prefs: &self.prefs,
            servers: &self.servers,
            colors: &self.colors,
            memory: &self.memory,
            retired: self
                .retired
                .iter()
                .map(|(port, r)| RetiredView {
                    port: *port,
                    name: r.name.clone(),
                    state: match r.phase {
                        Phase::Stopping => "stopping",
                        Phase::Stopped => "stopped",
                        Phase::Restarting => "restarting",
                    }
                    .into(),
                })
                .collect(),
            snoozed: &self.snoozed,
            github: &self.github,
            launch_at_login: autostart_path().exists(),
        });
        if state == self.state {
            return false;
        }
        self.json = serde_json::to_string(&state).unwrap_or_else(|_| "{}".into());
        self.state = state;
        true
    }

    /// Why a stop or restart of `port` cannot happen, if it cannot. Shared by the D-Bus
    /// methods so callers get an error instead of a silent success.
    fn check_stop(&self, port: u16, restart: bool) -> Result<(), String> {
        let Some(s) = self.server(port) else {
            return Err(format!("no dev server on :{port}"));
        };
        if s.is_protected {
            return Err(format!(":{port} is protected ({})", s.process_name));
        }
        if restart && !control::can_restart(s) {
            return Err(format!("don't know how to restart :{port}"));
        }
        Ok(())
    }

    fn begin_stop(&mut self, port: u16, restart: bool) -> Option<StopJob> {
        if self.retired.get(&port).is_some_and(|r| r.phase == Phase::Stopping) {
            return None;
        }
        let s = self.server(port)?.clone();
        if s.is_protected || (restart && !control::can_restart(&s)) {
            return None;
        }
        self.retired.insert(port, Retired { name: s.display_name(), phase: Phase::Stopping, since: wtp_core::now() });
        Some(StopJob {
            port,
            targets: s.process_starts.clone(),
            grace: if restart { 2.0 } else { self.prefs.force_quit_seconds.clamp(0.5, 30.0) },
            restart: restart.then_some(s),
        })
    }

    fn finish_stop(&mut self, port: u16, phase: Phase) {
        if let Some(r) = self.retired.get_mut(&port) {
            r.phase = phase;
            r.since = wtp_core::now();
        }
    }

    fn link(&self, port: u16, link: Link) -> Option<String> {
        let s = self.server(port)?;
        if let Link::Local = link {
            return Some(s.url());
        }
        let key = format!("{}|{}", s.project.root.as_deref()?, s.project.branch.as_deref()?);
        let info = &self.github.get(&key)?.0;
        match link {
            Link::PullRequest => info.pull_request.as_ref().map(|p| p.url.clone()),
            _ => info.preview.as_ref().map(|p| p.url.clone()),
        }
    }

    fn cleanup_ports(&self) -> Vec<u16> {
        let now = wtp_core::now();
        self.servers
            .iter()
            .filter(|s| model::cleanup_reason(s, &self.prefs, now).is_some_and(|r| !r.is_leak()))
            .map(|s| s.port)
            .collect()
    }

    fn set_pref(&mut self, key: &str, value: serde_json::Value) -> Result<(), String> {
        if key == "launch_at_login" {
            return set_autostart(value.as_bool().ok_or("launch_at_login must be a boolean")?).map_err(|e| e.to_string());
        }
        self.prefs.set(key, value)?;
        self.prefs.save().map_err(|e| e.to_string())
    }
}

fn autostart_path() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| wtp_core::home_dir().join(".config"))
        .join("autostart/dev.whattheport.WhatThePort.desktop")
}

fn set_autostart(enabled: bool) -> std::io::Result<()> {
    let path = autostart_path();
    if !enabled {
        return match fs::remove_file(&path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        };
    }
    let exe = std::env::current_exe()?;
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    fs::write(
        path,
        format!(
            "[Desktop Entry]\nType=Application\nName=WhatThePort\nComment=Dev servers in your top bar\nExec={} daemon\nIcon=whattheport\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n",
            exe.display()
        ),
    )
}

struct Bus {
    core: Arc<Mutex<Core>>,
    tx: mpsc::UnboundedSender<Command>,
    wake: Arc<Notify>,
}

fn port(p: u32) -> zbus::fdo::Result<u16> {
    u16::try_from(p).map_err(|_| zbus::fdo::Error::InvalidArgs(format!("bad port {p}")))
}

#[zbus::interface(name = "dev.whattheport.Daemon1")]
impl Bus {
    fn get_state(&self) -> String {
        self.core.lock().unwrap().json.clone()
    }

    /// kind: "local", "pr" or "preview"
    fn open(&self, port_: u32, kind: &str) -> zbus::fdo::Result<()> {
        let link = match kind {
            "pr" => Link::PullRequest,
            "preview" => Link::Preview,
            _ => Link::Local,
        };
        let _ = self.tx.send(Command::Open(port(port_)?, link));
        Ok(())
    }

    fn stop(&self, port_: u32) -> zbus::fdo::Result<()> {
        let port = port(port_)?;
        self.core.lock().unwrap().check_stop(port, false).map_err(zbus::fdo::Error::Failed)?;
        let _ = self.tx.send(Command::Stop(port));
        Ok(())
    }

    fn restart(&self, port_: u32) -> zbus::fdo::Result<()> {
        let port = port(port_)?;
        self.core.lock().unwrap().check_stop(port, true).map_err(zbus::fdo::Error::Failed)?;
        let _ = self.tx.send(Command::Restart(port));
        Ok(())
    }

    fn snooze(&self, port_: u32) -> zbus::fdo::Result<()> {
        let _ = self.tx.send(Command::Snooze(port(port_)?));
        Ok(())
    }

    fn clean_up(&self) {
        let _ = self.tx.send(Command::CleanUp);
    }

    /// `value` is JSON, e.g. SetPref("threshold_gb", "4").
    fn set_pref(&self, key: &str, value: &str) -> zbus::fdo::Result<()> {
        let value = serde_json::from_str(value).map_err(|e| zbus::fdo::Error::InvalidArgs(e.to_string()))?;
        self.core.lock().unwrap().set_pref(key, value).map_err(zbus::fdo::Error::InvalidArgs)?;
        self.wake.notify_one();
        Ok(())
    }

    fn quit(&self) {
        let _ = self.tx.send(Command::Quit);
    }

    #[zbus(signal)]
    async fn state_changed(emitter: &SignalEmitter<'_>, state: &str) -> zbus::Result<()>;
}

fn tray_wanted(mode: TrayMode) -> bool {
    match mode {
        TrayMode::On => true,
        TrayMode::Off => false,
        TrayMode::Auto => !std::env::var("XDG_CURRENT_DESKTOP").unwrap_or_default().to_uppercase().contains("GNOME"),
    }
}

pub async fn run(force_tray: bool) -> Result<(), Box<dyn Error>> {
    let core = Arc::new(Mutex::new(Core::new()));
    let wake = Arc::new(Notify::new());
    let quit = Arc::new(Notify::new());
    let (tx, rx) = mpsc::unbounded_channel();

    let conn = zbus::connection::Builder::session()?
        .serve_at(OBJECT_PATH, Bus { core: core.clone(), tx: tx.clone(), wake: wake.clone() })?
        .build()
        .await?;
    // The builder's own name request queues behind an existing owner instead of failing.
    match conn.request_name_with_flags(BUS_NAME, zbus::fdo::RequestNameFlags::DoNotQueue.into()).await {
        Ok(_) => {}
        Err(zbus::Error::NameTaken) => {
            eprintln!("wtp: already running");
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    }

    let notifier = Notifier::new(&conn, tx.clone())
        .await
        .inspect_err(|e| eprintln!("wtp: notifications unavailable: {e}"))
        .ok();

    let tray_mode = core.lock().unwrap().prefs.tray;
    let tray = if force_tray || tray_wanted(tray_mode) {
        tray::spawn(tx.clone())
            .await
            .inspect_err(|e| eprintln!("wtp: tray unavailable: {e}"))
            .ok()
    } else {
        None
    };

    tokio::spawn(run_commands(rx, core.clone(), wake.clone(), quit.clone()));

    let emitter = SignalEmitter::new(&conn, OBJECT_PATH)?;
    let mut sigterm = signal(SignalKind::terminate())?;
    let mut sigint = signal(SignalKind::interrupt())?;

    loop {
        let c = core.clone();
        let (changed, json, state, out) = tokio::task::spawn_blocking(move || {
            let mut core = c.lock().unwrap();
            let out = core.scan();
            let changed = core.rebuild();
            (changed, core.json.clone(), core.state.clone(), out)
        })
        .await?;

        if let Some(n) = &notifier {
            for notice in out.notices {
                let n = n.clone();
                tokio::spawn(async move { n.send(notice).await });
            }
        }
        for p in out.auto_stop {
            let _ = tx.send(Command::Stop(p));
        }
        for (root, branch) in out.lookups {
            let (core, wake) = (core.clone(), wake.clone());
            let prefs = core.lock().unwrap().prefs.clone();
            tokio::spawn(async move {
                let (r, b) = (root.clone(), branch.clone());
                let info = tokio::task::spawn_blocking(move || github::lookup(&r, &b, prefs.github_pull_requests, prefs.vercel_previews)).await;
                if let Ok(info) = info {
                    core.lock().unwrap().github.insert(format!("{root}|{branch}"), (info, wtp_core::now()));
                    wake.notify_one();
                }
            });
        }

        if changed {
            if let Err(e) = Bus::state_changed(&emitter, &json).await {
                eprintln!("wtp: signal failed: {e}");
            }
            if let Some(t) = &tray {
                t.update(move |t| t.state = state).await;
            }
        }

        let interval = core.lock().unwrap().prefs.scan_interval.clamp(1.0, 60.0);
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs_f64(interval)) => {}
            _ = wake.notified() => {}
            _ = quit.notified() => break,
            _ = sigterm.recv() => break,
            _ = sigint.recv() => break,
        }
    }

    if let Some(t) = tray {
        t.shutdown().await;
    }
    Ok(())
}

async fn run_commands(
    mut rx: mpsc::UnboundedReceiver<Command>,
    core: Arc<Mutex<Core>>,
    wake: Arc<Notify>,
    quit: Arc<Notify>,
) {
    while let Some(cmd) = rx.recv().await {
        match cmd {
            Command::Open(port, link) => {
                if let Some(url) = core.lock().unwrap().link(port, link) {
                    control::open_url(&url);
                }
            }
            Command::Stop(port) | Command::Restart(port) => {
                let restart = matches!(cmd, Command::Restart(_));
                let job = core.lock().unwrap().begin_stop(port, restart);
                if let Some(job) = job {
                    tokio::spawn(stop_job(job, core.clone(), wake.clone()));
                }
            }
            Command::CleanUp => {
                let ports = core.lock().unwrap().cleanup_ports();
                for port in ports {
                    let job = core.lock().unwrap().begin_stop(port, false);
                    if let Some(job) = job {
                        tokio::spawn(stop_job(job, core.clone(), wake.clone()));
                    }
                }
            }
            Command::Snooze(port) => {
                let mut c = core.lock().unwrap();
                if let Some(key) = c.server(port).map(|s| s.key.clone()) {
                    c.snoozed.insert(key, f64::INFINITY);
                }
            }
            Command::SetPref(key, value) => {
                if let Err(e) = core.lock().unwrap().set_pref(&key, value) {
                    eprintln!("wtp: {e}");
                }
            }
            Command::Quit => {
                quit.notify_one();
                return;
            }
        }
        wake.notify_one();
    }
}

async fn stop_job(job: StopJob, core: Arc<Mutex<Core>>, wake: Arc<Notify>) {
    wake.notify_one();
    control::terminate(&job.targets);
    let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(job.grace);
    while tokio::time::Instant::now() < deadline && control::signal_all(&job.targets, 0) > 0 {
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
    control::kill_survivors(&job.targets);

    let phase = match &job.restart {
        None => Phase::Stopped,
        Some(server) => {
            for _ in 0..20 {
                if control::is_port_free(job.port) {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            match control::spawn(server) {
                Ok(()) => Phase::Restarting,
                Err(e) => {
                    eprintln!("wtp: restart of :{} failed: {e}", job.port);
                    Phase::Stopped
                }
            }
        }
    };
    core.lock().unwrap().finish_stop(job.port, phase);
    wake.notify_one();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stopping_a_missing_port_is_an_error() {
        let core = Core::new();
        assert_eq!(core.check_stop(9, false).unwrap_err(), "no dev server on :9");
        assert_eq!(core.check_stop(9, true).unwrap_err(), "no dev server on :9");
    }
}
