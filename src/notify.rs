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
}

type Actions = Vec<(&'static str, Command)>;

#[derive(Clone)]
pub struct Notifier {
    proxy: NotificationsProxy<'static>,
    pending: Arc<Mutex<HashMap<u32, Actions>>>,
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
        Ok(Self { proxy, pending })
    }

    pub async fn send(&self, notice: Notice) {
        let mut actions: Vec<&str> = Vec::new();
        for (key, cmd) in &notice.actions {
            actions.push(key);
            actions.push(label(cmd));
        }
        let mut hints = HashMap::new();
        hints.insert("urgency", Value::U8(if notice.critical { 2 } else { 1 }));
        hints.insert("desktop-entry", Value::from("dev.whattheport.WhatThePort"));
        match self
            .proxy
            .notify("WhatThePort", 0, "whattheport", &notice.summary, &notice.body, &actions, hints, -1)
            .await
        {
            Ok(id) if !notice.actions.is_empty() => {
                // Dismissed notifications never report back, so keep the map bounded.
                let mut pending = self.pending.lock().unwrap();
                if pending.len() > 32 {
                    pending.clear();
                }
                pending.insert(id, notice.actions);
            }
            Ok(_) => {}
            Err(e) => eprintln!("wtp: notification failed: {e}"),
        }
    }
}
