//! Desktop notifications with action buttons, over org.freedesktop.Notifications.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use futures_util::StreamExt;
use tokio::sync::mpsc::UnboundedSender;
use zbus::zvariant::Value;

use crate::daemon::Command;

#[zbus::proxy(
    interface = "org.freedesktop.Notifications",
    default_service = "org.freedesktop.Notifications",
    default_path = "/org/freedesktop/Notifications"
)]
trait Notifications {
    #[allow(clippy::too_many_arguments)]
    fn notify(
        &self,
        app_name: &str,
        replaces_id: u32,
        app_icon: &str,
        summary: &str,
        body: &str,
        actions: &[&str],
        hints: HashMap<&str, Value<'_>>,
        expire_timeout: i32,
    ) -> zbus::Result<u32>;

    #[zbus(signal)]
    fn action_invoked(&self, id: u32, action_key: String) -> zbus::Result<()>;
}

pub struct Notice {
    pub summary: String,
    pub body: String,
    pub critical: bool,
    pub actions: Actions,
    /// When set, a later notice for the same key updates this bubble instead of stacking another.
    pub replace_key: Option<String>,
}

/// Notification id to pass as `replaces_id`. Zero means a new bubble.
pub fn replaces_id(known: &HashMap<String, u32>, key: Option<&str>) -> u32 {
    key.and_then(|k| known.get(k).copied()).unwrap_or(0)
}

type Actions = Vec<(&'static str, Command)>;

#[derive(Clone)]
pub struct Notifier {
    proxy: NotificationsProxy<'static>,
    pending: Arc<Mutex<HashMap<u32, Actions>>>,
    replaces: Arc<Mutex<HashMap<String, u32>>>,
}

fn label(cmd: &Command) -> &'static str {
    match cmd {
        Command::Stop(_) => "Stop",
        Command::Snooze(_) => "Mute alerts",
        Command::CleanUp => "Clean up",
        _ => "Open",
    }
}

impl Notifier {
    pub async fn new(conn: &zbus::Connection, tx: UnboundedSender<Command>) -> zbus::Result<Self> {
        let proxy = NotificationsProxy::new(conn).await?;
        let pending: Arc<Mutex<HashMap<u32, Actions>>> = Default::default();
        let replaces: Arc<Mutex<HashMap<String, u32>>> = Default::default();
        let mut stream = proxy.receive_action_invoked().await?;
        let map = pending.clone();
        tokio::spawn(async move {
            while let Some(signal) = stream.next().await {
                let Ok(args) = signal.args() else { continue };
                let actions = map.lock().unwrap().remove(&args.id);
                if let Some((_, cmd)) = actions.into_iter().flatten().find(|(k, _)| *k == args.action_key) {
                    let _ = tx.send(cmd);
                }
            }
        });
        Ok(Self { proxy, pending, replaces })
    }

    pub async fn send(&self, notice: Notice) {
        let mut actions: Vec<&str> = Vec::new();
        for (key, cmd) in &notice.actions {
            actions.push(key);
            actions.push(label(cmd));
        }
        let replaces_id = replaces_id(&self.replaces.lock().unwrap(), notice.replace_key.as_deref());
        let mut hints = HashMap::new();
        hints.insert("urgency", Value::U8(if notice.critical { 2 } else { 1 }));
        hints.insert("desktop-entry", Value::from("dev.whattheport.WhatThePort"));
        match self
            .proxy
            .notify("WhatThePort", replaces_id, "whattheport", &notice.summary, &notice.body, &actions, hints, -1)
            .await
        {
            Ok(id) => {
                if let Some(key) = notice.replace_key.clone() {
                    let old = self.replaces.lock().unwrap().insert(key, id);
                    if let Some(old) = old {
                        self.pending.lock().unwrap().remove(&old);
                    }
                }
                if !notice.actions.is_empty() {
                    // Dismissed notifications never report back, so keep the map bounded.
                    let mut pending = self.pending.lock().unwrap();
                    if pending.len() > 32 {
                        pending.clear();
                    }
                    pending.insert(id, notice.actions);
                }
            }
            Err(e) => eprintln!("wtp: notification failed: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_repeat_replaces_the_previous_bubble() {
        let mut known = HashMap::new();
        assert_eq!(replaces_id(&known, Some("5173-1-1")), 0);
        known.insert("5173-1-1".into(), 42);
        assert_eq!(replaces_id(&known, Some("5173-1-1")), 42);
        assert_eq!(replaces_id(&known, None), 0);
        assert_eq!(replaces_id(&known, Some("other")), 0);
    }
}
