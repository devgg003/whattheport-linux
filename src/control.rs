use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::Duration;

use crate::model::Server;
use crate::procfs::{self, Args};

/// Signal every process we scanned for this server, skipping any PID that has
/// since been reused by an unrelated process.
pub fn signal_all(targets: &HashMap<i32, f64>, signal: i32) -> usize {
    let own = std::process::id() as i32;
    let mut sent = 0;
    for (pid, started) in targets {
        if *pid <= 1 || *pid == own {
            continue;
        }
        if procfs::process(*pid).is_some_and(|p| (p.start_time - started).abs() < 1.0)
            && unsafe { libc::kill(*pid, signal) } == 0
        {
            sent += 1;
        }
    }
    sent
}

pub fn terminate(targets: &HashMap<i32, f64>) -> usize {
    signal_all(targets, libc::SIGTERM)
}

pub fn kill_survivors(targets: &HashMap<i32, f64>) -> usize {
    signal_all(targets, libc::SIGKILL)
}

pub fn is_port_free(port: u16) -> bool {
    let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_err()
}

/// Rebuild a shell command line that re-launches the server the way it was started.
pub fn shell_command(launch: &Args) -> Option<String> {
    let args: Vec<&str> = launch.argv.iter().map(|a| a.trim()).filter(|a| !a.is_empty()).collect();
    let first = *args.first()?;
    let exe = first.rsplit('/').next().unwrap_or(first);
    if matches!(exe, "sh" | "bash" | "zsh" | "dash") && args.len() >= 3 && args[1] == "-c" {
        return Some(args[2].into());
    }
    if args.len() == 1 && first.contains(' ') {
        // Processes that rewrote their title, e.g. "next-server (v15.2.0)".
        return Some(first.split(" (").next().unwrap_or(first).into());
    }
    let executable = if first.starts_with('/') { first } else { launch.exe.as_str() };
    Some(
        std::iter::once(executable)
            .chain(args[1..].iter().copied())
            .map(shell_quote)
            .collect::<Vec<_>>()
            .join(" "),
    )
}

fn shell_quote(s: &str) -> String {
    if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c)) {
        s.into()
    } else {
        format!("'{}'", s.replace('\'', r"'\''"))
    }
}

pub fn can_restart(s: &Server) -> bool {
    !s.is_protected
        && s.launch.as_ref().and_then(shell_command).is_some()
        && s.launch_directory.as_deref().is_some_and(|d| std::path::Path::new(d).is_dir())
}

/// Start the server again, detached, logging to ~/.local/state/whattheport/port-N.log.
pub fn spawn(s: &Server) -> std::io::Result<()> {
    let launch = s.launch.as_ref().ok_or_else(|| std::io::Error::other("no launch command"))?;
    let cmd = shell_command(launch).ok_or_else(|| std::io::Error::other("no launch command"))?;
    let dir = s.launch_directory.as_deref().ok_or_else(|| std::io::Error::other("no directory"))?;
    let logs = crate::home_dir().join(".local/state/whattheport");
    fs::create_dir_all(&logs)?;
    let log = OpenOptions::new().create(true).append(true).open(logs.join(format!("port-{}.log", s.port)))?;
    Command::new("/bin/bash")
        .arg("-c")
        .arg(cmd)
        .current_dir(dir)
        .envs(&launch.env)
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log)
        .process_group(0)
        .spawn()?;
    Ok(())
}

pub fn open_url(url: &str) {
    let _ = Command::new("xdg-open")
        .arg(url)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(exe: &str, argv: &[&str]) -> Args {
        Args { exe: exe.into(), argv: argv.iter().map(|s| s.to_string()).collect(), env: Default::default() }
    }

    #[test]
    fn rebuilds_commands() {
        assert_eq!(shell_command(&args("/bin/sh", &["sh", "-c", "next dev -p 3000"])).unwrap(), "next dev -p 3000");
        assert_eq!(shell_command(&args("/usr/bin/node", &["next-server (v15.2.0)"])).unwrap(), "next-server");
        assert_eq!(
            shell_command(&args("/usr/bin/python3", &["python3", "-m", "http.server", "it's"])).unwrap(),
            r"/usr/bin/python3 -m http.server 'it'\''s'"
        );
    }
}
