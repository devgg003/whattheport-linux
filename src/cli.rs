use std::collections::HashMap;
use std::error::Error;
use std::time::Duration;

use wtp_core::control;
use wtp_core::prefs::Prefs;
use wtp_core::scan::ScanEngine;

use crate::daemon::{BUS_NAME, INTERFACE, OBJECT_PATH};
use crate::view::{self, State};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

/// A connection to the running service, if there is one. Never auto-starts it.
async fn daemon() -> Option<zbus::Connection> {
    let conn = zbus::Connection::session().await.ok()?;
    let dbus = zbus::fdo::DBusProxy::new(&conn).await.ok()?;
    dbus.name_has_owner(BUS_NAME.try_into().ok()?).await.ok()?.then_some(conn)
}

async fn call<B>(conn: &zbus::Connection, method: &str, body: &B) -> Result<zbus::Message>
where
    B: serde::Serialize + zbus::zvariant::DynamicType,
{
    Ok(conn.call_method(Some(BUS_NAME), OBJECT_PATH, Some(INTERFACE), method, body).await?)
}

fn local_scan() -> (Prefs, Vec<wtp_core::model::Server>) {
    let prefs = Prefs::load();
    let mut engine = ScanEngine::default();
    engine.scan(&prefs);
    // A second sample gives CPU usage a time window to measure over.
    std::thread::sleep(Duration::from_millis(400));
    let servers = engine.scan(&prefs);
    (prefs, servers)
}

fn local_state() -> State {
    let (prefs, servers) = local_scan();
    let colors: HashMap<u16, usize> = servers.iter().enumerate().map(|(i, s)| (s.port, i)).collect();
    view::build(view::Inputs {
        now: wtp_core::now(),
        prefs: &prefs,
        servers: &servers,
        colors: &colors,
        memory: &wtp_core::procfs::system_memory(),
        retired: Vec::new(),
        snoozed: &HashMap::new(),
        github: &HashMap::new(),
        launch_at_login: false,
    })
}

async fn state_json() -> Result<String> {
    match daemon().await {
        Some(conn) => Ok(call(&conn, "GetState", &()).await?.body().deserialize::<String>()?),
        None => Ok(serde_json::to_string(&local_state())?),
    }
}

pub async fn list(json: bool) -> Result<()> {
    let raw = state_json().await?;
    if json {
        // The panel menu loaded at login reads this as a list. The full object
        // stays on the daemon's GetState method for the new menu.
        let state: serde_json::Value = serde_json::from_str(&raw)?;
        let list = state["servers"].as_array().cloned().unwrap_or_default();
        println!("{}", serde_json::to_string_pretty(&list)?);
        return Ok(());
    }
    let state: serde_json::Value = serde_json::from_str(&raw)?;
    let tty = unsafe { libc::isatty(1) } == 1;
    let paint = |code: &str, text: &str| if tty { format!("\x1b[{code}m{text}\x1b[0m") } else { text.to_string() };

    let servers = state["servers"].as_array().cloned().unwrap_or_default();
    if servers.is_empty() {
        println!("No dev servers listening.");
        return Ok(());
    }
    let width = servers.iter().filter_map(|s| s["name"].as_str()).map(|n| n.chars().count()).max().unwrap_or(4).clamp(4, 32);
    for s in &servers {
        let name: String = s["name"].as_str().unwrap_or("").chars().take(width).collect();
        let memory = format!("{:>9}", s["memory"].as_str().unwrap_or(""));
        let memory = if s["attention"].as_bool() == Some(true) { paint("33;1", &memory) } else { memory };
        let mut detail = s["subtitle"].as_str().unwrap_or("").to_string();
        if let Some(c) = s["cleanup"].as_str() {
            detail.push_str(&format!(" · {}", paint("2", &format!("clean up: {c}"))));
        }
        println!(
            "{}  {name:<width$}  {memory}  {:>6}  {}",
            paint("1", &format!(":{:<5}", s["port"])),
            s["cpu"].as_str().unwrap_or(""),
            paint("2", &detail),
        );
    }
    let sum = &state["summary"];
    println!(
        "\n{} in servers · {} other · {} free · CPU {}",
        sum["servers_memory"].as_str().unwrap_or(""),
        sum["other_memory"].as_str().unwrap_or(""),
        sum["free_memory"].as_str().unwrap_or(""),
        sum["cpu"].as_str().unwrap_or("")
    );
    Ok(())
}

pub async fn act(action: &str, port: u16) -> Result<()> {
    if let Some(conn) = daemon().await {
        match action {
            "open" => call(&conn, "Open", &(port as u32, "local")).await?,
            "stop" => call(&conn, "Stop", &(port as u32)).await?,
            _ => call(&conn, "Restart", &(port as u32)).await?,
        };
        return Ok(());
    }
    if action == "open" {
        control::open_url(&format!("http://localhost:{port}"));
        return Ok(());
    }
    let (prefs, servers) = local_scan();
    let server = servers.into_iter().find(|s| s.port == port).ok_or(format!("no dev server on :{port}"))?;
    if server.is_protected {
        return Err(format!(":{port} is protected ({})", server.process_name).into());
    }
    if action == "restart" && !control::can_restart(&server) {
        return Err(format!("don't know how to restart :{port}").into());
    }
    control::terminate(&server.process_starts);
    let grace = if action == "restart" { 2.0 } else { prefs.force_quit_seconds };
    let deadline = std::time::Instant::now() + Duration::from_secs_f64(grace);
    while std::time::Instant::now() < deadline && control::signal_all(&server.process_starts, 0) > 0 {
        std::thread::sleep(Duration::from_millis(150));
    }
    control::kill_survivors(&server.process_starts);
    if action == "restart" {
        for _ in 0..20 {
            if control::is_port_free(port) {
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        control::spawn(&server)?;
    }
    Ok(())
}

pub async fn quit() -> Result<()> {
    match daemon().await {
        Some(conn) => {
            call(&conn, "Quit", &()).await?;
            Ok(())
        }
        None => Err("not running".into()),
    }
}
