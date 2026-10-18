//! StatusNotifierItem tray for KDE, XFCE, Cinnamon and friends. Those menus only
//! support text, checkmarks and submenus, so each server becomes a submenu.

use ksni::menu::{CheckmarkItem, RadioGroup, RadioItem, StandardItem, SubMenu};
use ksni::{MenuItem, ToolTip, TrayMethods};
use serde_json::Value;
use tokio::sync::mpsc::UnboundedSender;

use crate::daemon::{Command, Link};
use crate::view::{ServerView, State};

pub struct Tray {
    pub state: State,
    tx: UnboundedSender<Command>,
}

pub async fn spawn(tx: UnboundedSender<Command>) -> Result<ksni::Handle<Tray>, ksni::Error> {
    Tray { state: State::default(), tx }.spawn().await
}

const THRESHOLDS: &[f64] = &[1.0, 2.0, 4.0, 8.0];
const CLEANUP_MODES: &[(&str, &str)] = &[("off", "Off"), ("ask", "Ask first"), ("automatic", "Automatic")];

fn info(label: impl Into<String>) -> MenuItem<Tray> {
    StandardItem { label: label.into().replace('_', "__"), enabled: false, ..Default::default() }.into()
}

fn action(label: impl Into<String>, cmd: Command) -> MenuItem<Tray> {
    StandardItem {
        label: label.into(),
        activate: Box::new(move |t: &mut Tray| {
            let _ = t.tx.send(cmd.clone());
        }),
        ..Default::default()
    }
    .into()
}

fn toggle(label: &str, key: &'static str, checked: bool) -> MenuItem<Tray> {
    CheckmarkItem {
        label: label.into(),
        checked,
        activate: Box::new(move |t: &mut Tray| {
            let _ = t.tx.send(Command::SetPref(key.into(), Value::Bool(!checked)));
        }),
        ..Default::default()
    }
    .into()
}

fn server_menu(s: &ServerView) -> MenuItem<Tray> {
    let mut items = vec![info(&s.subtitle)];
    if let Some(cmd) = &s.command {
        items.push(info(cmd));
    }
    let mut stats = format!("CPU {}", s.cpu);
    if let Some(g) = &s.growth {
        stats.push_str(&format!(" · {g}"));
    }
    items.push(info(stats));
    if let Some(why) = &s.cleanup {
        items.push(info(format!("Suggest clean up: {why}")));
    }
    items.push(MenuItem::Separator);
    items.push(action(format!("Open {}", s.url), Command::Open(s.port, Link::Local)));
    if let Some(pr) = &s.pull_request {
        items.push(action(format!("Open PR #{} ({})", pr.number, pr.state), Command::Open(s.port, Link::PullRequest)));
    }
    if let Some(p) = &s.preview {
        items.push(action(format!("Open preview ({})", p.state), Command::Open(s.port, Link::Preview)));
    }
    if s.can_restart {
        items.push(action("Restart", Command::Restart(s.port)));
    }
    if !s.protected {
        items.push(action("Stop", Command::Stop(s.port)));
    }
    if s.attention && !s.snoozed {
        items.push(action("Mute alerts", Command::Snooze(s.port)));
    }
    let flag = if s.attention { "⚠ " } else { "" };
    SubMenu { label: format!("{flag}:{}  {}  —  {}", s.port, s.name, s.memory).replace('_', "__"), submenu: items, ..Default::default() }.into()
}

impl ksni::Tray for Tray {
    const MENU_ON_ACTIVATE: bool = true;

    fn id(&self) -> String {
        "whattheport".into()
    }

    fn title(&self) -> String {
        format!("WhatThePort ({} servers)", self.state.count)
    }

    fn icon_name(&self) -> String {
        if self.state.attention { "whattheport-attention" } else { "whattheport" }.into()
    }

    fn tool_tip(&self) -> ToolTip {
        let s = &self.state.summary;
        ToolTip {
            title: format!("{} servers · {} · CPU {}", self.state.count, s.servers_memory, s.cpu),
            description: self
                .state
                .servers
                .iter()
                .map(|v| format!(":{}  {}  {}", v.port, v.name, v.memory))
                .collect::<Vec<_>>()
                .join("\n"),
            ..Default::default()
        }
    }

    fn menu(&self) -> Vec<MenuItem<Self>> {
        let st = &self.state;
        let mut items = vec![info(format!(
            "Servers {} · Other {} · Free {}",
            st.summary.servers_memory, st.summary.other_memory, st.summary.free_memory
        ))];
        items.push(MenuItem::Separator);
        if st.servers.is_empty() && st.retired.is_empty() {
            items.push(info("No dev servers listening"));
        }
        items.extend(st.servers.iter().map(server_menu));
        items.extend(st.retired.iter().map(|r| info(format!(":{}  {}  {}", r.port, r.name, r.state))));
        items.push(MenuItem::Separator);
        if let Some(c) = &st.cleanup {
            items.push(action(&c.label, Command::CleanUp));
        }

        let p = &st.prefs;
        let mut thresholds = THRESHOLDS.to_vec();
        if !thresholds.contains(&p.threshold_gb) {
            thresholds.push(p.threshold_gb);
        }
        let threshold_values = thresholds.clone();
        let settings = vec![
            info("Memory alert above"),
            RadioGroup {
                selected: thresholds.iter().position(|t| *t == p.threshold_gb).unwrap_or(0),
                select: Box::new(move |t: &mut Tray, i| {
                    let _ = t.tx.send(Command::SetPref("threshold_gb".into(), threshold_values[i].into()));
                }),
                options: thresholds.iter().map(|t| RadioItem { label: format!("{t} GB"), ..Default::default() }).collect(),
            }
            .into(),
            MenuItem::Separator,
            info("Clean up idle servers"),
            RadioGroup {
                selected: CLEANUP_MODES.iter().position(|(k, _)| *k == p.cleanup_mode).unwrap_or(1),
                select: Box::new(|t: &mut Tray, i| {
                    let _ = t.tx.send(Command::SetPref("cleanup_mode".into(), CLEANUP_MODES[i].0.into()));
                }),
                options: CLEANUP_MODES.iter().map(|(_, l)| RadioItem { label: (*l).into(), ..Default::default() }).collect(),
            }
            .into(),
            MenuItem::Separator,
            toggle("Memory and leak alerts", "leak_warnings", p.leak_warnings),
            toggle("Show git branches", "show_branches", p.show_branches),
            toggle("Link Claude Code sessions", "link_claude", p.link_claude),
            toggle("Link Codex sessions", "link_codex", p.link_codex),
            toggle("Look up GitHub pull requests", "github_pull_requests", p.github_pull_requests),
            toggle("Look up Vercel previews", "vercel_previews", p.vercel_previews),
            MenuItem::Separator,
            toggle("Launch at login", "launch_at_login", p.launch_at_login),
        ];
        items.push(SubMenu { label: "Settings".into(), submenu: settings, ..Default::default() }.into());
        items.push(action("Quit WhatThePort", Command::Quit));
        items
    }
}
