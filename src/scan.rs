//! Turns a /proc + socket snapshot into the list of dev servers, keeping the
//! small amount of state needed between scans (CPU deltas, history, caches).

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use crate::agents::AgentResolver;
use crate::model::{Sample, Server, ServerProcess};
use crate::prefs::Prefs;
use crate::procfs::{self, Args, Proc};
use crate::project::{ProjectInfo, ProjectResolver};
use crate::rules;
use crate::sockets::{SocketScan, SocketScanner};
use crate::tree::ProcessTree;

pub const HISTORY_SECS: f64 = 600.0;

#[derive(Clone)]
struct DockerContainer {
    id: String,
    name: String,
    image: String,
}

#[derive(Default)]
pub struct ScanEngine {
    sockets: SocketScanner,
    projects: ProjectResolver,
    agents: AgentResolver,
    /// pid -> (start_time, cpu_ticks, sampled_at)
    prev_cpu: HashMap<i32, (f64, u64, f64)>,
    histories: HashMap<String, Vec<Sample>>,
    last_active: HashMap<String, f64>,
    args_cache: HashMap<i32, (f64, Option<Args>)>,
    docker_cache: (f64, HashMap<u16, DockerContainer>),
}

impl ScanEngine {
    pub fn scan(&mut self, prefs: &Prefs) -> Vec<Server> {
        let sockets = self.sockets.scan(prefs.min_port..=prefs.max_port);
        let processes = procfs::all_processes();
        self.scan_snapshot(prefs, &sockets, &processes, crate::now())
    }

    pub fn scan_snapshot(
        &mut self,
        prefs: &Prefs,
        sockets: &SocketScan,
        processes: &HashMap<i32, Proc>,
        now: f64,
    ) -> Vec<Server> {
        let tree = ProcessTree::new(processes, sockets);
        let allowlist = prefs.allowlist_set();
        let protected = prefs.protected_set();
        let mut sampled: HashMap<i32, (u64, f64)> = HashMap::new();
        let mut servers = Vec::new();
        let mut seen_keys = HashSet::new();
        let mut seen_ports = HashSet::new();
        let mut live_pids = HashSet::new();

        for socket in &sockets.listening {
            if socket.port < prefs.min_port || socket.port > prefs.max_port || seen_ports.contains(&socket.port) {
                continue;
            }
            let Some(listener) = processes.get(&socket.pid) else { continue };
            if !self.is_allowed_listener(&socket.command, listener, &allowlist) {
                continue;
            }
            seen_ports.insert(socket.port);

            let service = tree.service(listener.pid);
            let root = self.root_process(listener, processes, &tree, &service);
            let members = tree.descendants(root.pid, &service);

            let mut total_mem = 0;
            let mut total_cpu = 0.0;
            let mut nodes = Vec::with_capacity(members.len());
            let mut starts = HashMap::new();
            for (proc, depth) in &members {
                live_pids.insert(proc.pid);
                let (mem, cpu) = *sampled
                    .entry(proc.pid)
                    .or_insert_with(|| (procfs::rss(proc.pid), self.cpu_percent(proc, now)));
                total_mem += mem;
                total_cpu += cpu;
                starts.insert(proc.pid, proc.start_time);
                nodes.push(ServerProcess {
                    pid: proc.pid,
                    name: self.display_name(proc),
                    depth: *depth,
                    memory: mem,
                    cpu,
                });
            }

            let is_docker = socket.command == "docker-proxy";
            let cwd = procfs::cwd(listener.pid).or_else(|| procfs::cwd(root.pid));
            if cwd.as_deref() == Some("/") && !is_docker {
                continue;
            }

            let env = self.inherited_env(listener, root, processes);
            let mut command = self
                .args(root)
                .map(|a| pretty_command(&a.argv, &root.comm));

            let project = if is_docker {
                match self.docker_container(socket.port, now) {
                    Some(c) => {
                        self.populate_docker_metrics(
                            &c.id,
                            processes,
                            &tree,
                            now,
                            &mut sampled,
                            &mut live_pids,
                            &mut starts,
                            &mut nodes,
                            &mut total_mem,
                            &mut total_cpu,
                        );
                        command = Some(format!("docker: {} ({})", c.name, c.image));
                        ProjectInfo {
                            name: c.name,
                            framework: Some(format!("Docker ({})", c.image)),
                            branch: Some("container".into()),
                            ..Default::default()
                        }
                    }
                    None => ProjectInfo {
                        name: format!("docker:{}", socket.port),
                        framework: Some("Docker".into()),
                        ..Default::default()
                    },
                }
            } else {
                let mut p = self.projects.resolve(cwd.as_deref(), command.as_deref());
                if !prefs.show_branches {
                    p.branch = None;
                }
                p
            };

            // The first ancestor (from the root down) whose argv is intact is what we re-run on restart.
            let mut chain = vec![listener];
            let mut cur = listener;
            while cur.pid != root.pid {
                match processes.get(&cur.ppid) {
                    Some(parent) if !chain.iter().any(|p| p.pid == parent.pid) => {
                        chain.push(parent);
                        cur = parent;
                    }
                    _ => break,
                }
            }
            let launcher = chain
                .iter()
                .rev()
                .find(|p| self.args(p).is_some_and(|a| has_intact_args(&a)))
                .copied()
                .unwrap_or(root);

            let key = format!("{}-{}-{}", socket.port, root.pid, root.start_time as u64);
            seen_keys.insert(key.clone());
            let connections = sockets.inbound.get(&socket.port).copied().unwrap_or(0);
            if total_cpu >= 2.0 || connections > 0 || !self.last_active.contains_key(&key) {
                self.last_active.insert(key.clone(), now);
            }
            let history = self.histories.entry(key.clone()).or_default();
            history.push(Sample { time: now, memory: total_mem, cpu: total_cpu });
            history.retain(|s| s.time >= now - HISTORY_SECS);
            let history = history.clone();

            let agent = self.agents.resolve(
                &env,
                cwd.as_deref(),
                root.start_time,
                command.as_deref(),
                prefs.link_claude,
                prefs.link_codex,
            );
            let is_protected = protected.contains(&socket.command)
                || command.as_deref().is_some_and(|c| protected.iter().any(|p| c.contains(p.as_str())));

            servers.push(Server {
                key: key.clone(),
                port: socket.port,
                pid: listener.pid,
                root_pid: root.pid,
                process_name: socket.command.clone(),
                addresses: socket.addresses.clone(),
                cwd_exists: cwd.as_deref().is_none_or(|c| Path::new(c).is_dir()),
                launch_directory: procfs::cwd(launcher.pid).or_else(|| cwd.clone()),
                cwd,
                command,
                launch: self.args(launcher),
                started_at: root.start_time,
                project,
                agent,
                processes: nodes,
                process_starts: starts,
                memory: total_mem,
                cpu: total_cpu,
                connections,
                history,
                last_active: self.last_active.get(&key).copied().unwrap_or(now),
                is_protected,
            });
        }

        self.histories.retain(|k, _| seen_keys.contains(k));
        self.last_active.retain(|k, _| seen_keys.contains(k));
        self.prev_cpu.retain(|k, _| live_pids.contains(k));
        self.args_cache.retain(|k, _| processes.contains_key(k));

        servers.sort_by_key(|s| s.port);
        servers
    }

    fn cpu_percent(&mut self, proc: &Proc, now: f64) -> f64 {
        let prev = self.prev_cpu.insert(proc.pid, (proc.start_time, proc.cpu_ticks, now));
        match prev {
            Some((start, ticks, at))
                if (start - proc.start_time).abs() < 1.0 && proc.cpu_ticks >= ticks && now > at =>
            {
                procfs::ticks_to_secs(proc.cpu_ticks - ticks) / (now - at) * 100.0
            }
            _ => 0.0,
        }
    }

    fn args(&mut self, proc: &Proc) -> Option<Args> {
        if let Some((start, args)) = self.args_cache.get(&proc.pid)
            && (start - proc.start_time).abs() < 1.0
        {
            return args.clone();
        }
        let args = procfs::args(proc.pid);
        self.args_cache.insert(proc.pid, (proc.start_time, args.clone()));
        args
    }

    fn root_process<'a>(
        &mut self,
        listener: &'a Proc,
        processes: &'a HashMap<i32, Proc>,
        tree: &ProcessTree,
        service: &HashSet<i32>,
    ) -> &'a Proc {
        let mut current = listener;
        let mut visited = HashSet::from([listener.pid]);
        loop {
            if current.ppid <= 1 {
                break;
            }
            let Some(parent) = processes.get(&current.ppid) else { break };
            if !visited.insert(parent.pid) || tree.contains_other_service(parent.pid, service) {
                break;
            }
            if self.is_runner(parent) {
                current = parent;
                continue;
            }
            // Step over the `sh -c` a package manager puts between itself and the server.
            if rules::SHELLS.contains(&parent.comm.as_str())
                && parent.ppid > 1
                && let Some(grand) = processes.get(&parent.ppid)
                && self.is_runner(grand)
                && visited.insert(grand.pid)
            {
                if tree.contains_other_service(grand.pid, service) {
                    current = parent;
                    break;
                }
                current = grand;
                continue;
            }
            break;
        }
        current
    }

    fn is_runner(&mut self, proc: &Proc) -> bool {
        self.is_proc_allowed(proc, |c| rules::RUNNERS.contains(&c)) && !self.is_agent(proc)
    }

    fn is_agent(&mut self, proc: &Proc) -> bool {
        if rules::AGENT_NAMES.contains(&proc.comm.as_str()) {
            return true;
        }
        let Some(args) = self.args(proc) else { return false };
        let joined = std::iter::once(args.exe.as_str())
            .chain(args.argv.iter().take(3).map(String::as_str))
            .collect::<Vec<_>>()
            .join(" ");
        rules::AGENT_PATH_MARKERS.iter().any(|m| joined.contains(m))
    }

    fn is_allowed_listener(&mut self, command: &str, listener: &Proc, allow: &HashSet<String>) -> bool {
        if let Some(args) = self.args(listener) {
            let excluded = |s: &str| rules::EXCLUDED_DESKTOP_BINARIES.contains(&s);
            let arg0 = args.argv.first().map(|a| basename(a)).unwrap_or("");
            if excluded(basename(&args.exe)) || excluded(arg0) || excluded(command) {
                return false;
            }
        }
        let listed = |c: &str| allow.contains(c);
        is_allowed(command, listed) || self.is_proc_allowed(listener, listed)
    }

    fn is_proc_allowed(&mut self, proc: &Proc, allow: impl Fn(&str) -> bool + Copy) -> bool {
        if is_allowed(&proc.comm, allow) {
            return true;
        }
        let Some(args) = self.args(proc) else { return false };
        let excluded = |s: &str| rules::EXCLUDED_DESKTOP_BINARIES.contains(&s);
        if !args.exe.is_empty() {
            let exe = basename(&args.exe);
            if excluded(exe) {
                return false;
            }
            if is_allowed(exe, allow) {
                return true;
            }
        }
        if let Some(first) = args.argv.first() {
            let arg0 = basename(first.split_whitespace().next().unwrap_or(""));
            if excluded(arg0) {
                return false;
            }
            if is_allowed(arg0, allow) {
                return true;
            }
        }
        false
    }

    /// Environment as seen by the server: merged from a few ancestors up past the root,
    /// so variables set by the agent or shell that launched it are included.
    fn inherited_env(&mut self, listener: &Proc, root: &Proc, processes: &HashMap<i32, Proc>) -> HashMap<String, String> {
        let mut chain = vec![listener];
        let mut cur = listener;
        let mut extra_hops = 2;
        let mut passed_root = false;
        while cur.ppid > 1 && chain.len() < 10 {
            let Some(parent) = processes.get(&cur.ppid) else { break };
            passed_root |= cur.pid == root.pid;
            if passed_root {
                if extra_hops == 0 {
                    break;
                }
                extra_hops -= 1;
            }
            chain.push(parent);
            cur = parent;
        }
        let mut env = HashMap::new();
        for p in chain.into_iter().rev() {
            if let Some(a) = self.args(p) {
                env.extend(a.env);
            }
        }
        env
    }

    fn display_name(&mut self, proc: &Proc) -> String {
        match self.args(proc) {
            Some(a) if !a.argv.is_empty() => {
                let cmd = pretty_command(&a.argv, &proc.comm);
                match cmd.find(" (") {
                    Some(i) => cmd[..i].to_string(),
                    None => cmd,
                }
            }
            _ => proc.comm.clone(),
        }
    }

    fn docker_container(&mut self, port: u16, now: f64) -> Option<DockerContainer> {
        if now - self.docker_cache.0 > 10.0 {
            self.docker_cache = (now, docker_port_map().unwrap_or_default());
        }
        self.docker_cache.1.get(&port).cloned()
    }

    #[allow(clippy::too_many_arguments)]
    fn populate_docker_metrics(
        &mut self,
        container_id: &str,
        processes: &HashMap<i32, Proc>,
        tree: &ProcessTree,
        now: f64,
        sampled: &mut HashMap<i32, (u64, f64)>,
        live_pids: &mut HashSet<i32>,
        starts: &mut HashMap<i32, f64>,
        nodes: &mut Vec<ServerProcess>,
        total_mem: &mut u64,
        total_cpu: &mut f64,
    ) {
        let Some((cg_pids, cg_mem)) = docker_cgroup_stats(container_id) else {
            return;
        };
        let pid_set: HashSet<i32> = cg_pids.iter().copied().collect();
        if pid_set.is_empty() {
            if let Some(m) = cg_mem.filter(|m| *m > *total_mem) {
                *total_mem = m;
                if let Some(first) = nodes.first_mut() {
                    first.memory = m;
                }
            }
            return;
        }

        // Find the root process inside the container (the one whose parent is not in the container).
        let root_pid = cg_pids
            .iter()
            .copied()
            .find(|pid| processes.get(pid).is_none_or(|p| !pid_set.contains(&p.ppid)))
            .unwrap_or(cg_pids[0]);

        let ordered: Vec<(&Proc, usize)> = if processes.contains_key(&root_pid) {
            tree.descendants(root_pid, &pid_set)
                .into_iter()
                .filter(|(p, _)| pid_set.contains(&p.pid))
                .collect()
        } else {
            cg_pids
                .iter()
                .filter_map(|pid| processes.get(pid).map(|p| (p, usize::from(*pid != root_pid))))
                .collect()
        };

        if ordered.is_empty() {
            return;
        }

        nodes.clear();
        *total_mem = 0;
        *total_cpu = 0.0;
        for (proc, depth) in ordered {
            live_pids.insert(proc.pid);
            let (mem, cpu) = *sampled
                .entry(proc.pid)
                .or_insert_with(|| (procfs::rss(proc.pid), self.cpu_percent(proc, now)));
            *total_mem += mem;
            *total_cpu += cpu;
            starts.insert(proc.pid, proc.start_time);
            nodes.push(ServerProcess {
                pid: proc.pid,
                name: self.display_name(proc),
                depth,
                memory: mem,
                cpu,
            });
        }

        // If cgroup memory.current exceeds raw RSS sum, attribute the remainder to the container root
        // so both the server total and the sum across s.processes match cgroup memory usage.
        if let Some(m) = cg_mem.filter(|m| *m > *total_mem) {
            let extra = m - *total_mem;
            *total_mem = m;
            if let Some(first) = nodes.first_mut() {
                first.memory += extra;
                sampled.insert(first.pid, (first.memory, first.cpu));
            }
        }
    }
}

fn docker_cgroup_stats(container_id: &str) -> Option<(Vec<i32>, Option<u64>)> {
    if container_id.is_empty() {
        return None;
    }
    let candidates = [
        format!("/sys/fs/cgroup/system.slice/docker-{container_id}.scope"),
        format!("/sys/fs/cgroup/docker/{container_id}"),
    ];
    for dir in candidates {
        let procs_path = format!("{dir}/cgroup.procs");
        if let Ok(text) = std::fs::read_to_string(&procs_path) {
            let pids: Vec<i32> = text
                .lines()
                .filter_map(|l| l.trim().parse::<i32>().ok())
                .collect();
            let mem = std::fs::read_to_string(format!("{dir}/memory.current"))
                .ok()
                .and_then(|s| s.trim().parse::<u64>().ok());
            return Some((pids, mem));
        }
    }
    None
}

fn basename(p: &str) -> &str {
    p.rsplit('/').next().unwrap_or(p)
}

fn is_allowed(command: &str, allow: impl Fn(&str) -> bool) -> bool {
    if command.is_empty() {
        return false;
    }
    if allow(command) {
        return true;
    }
    allow("python3")
        && command
            .strip_prefix("python3.")
            .is_some_and(|v| !v.is_empty() && v.chars().all(|c| c.is_ascii_digit() || c == '.'))
}

fn has_intact_args(args: &Args) -> bool {
    let parts: Vec<&str> = args.argv.iter().map(|a| a.trim()).filter(|a| !a.is_empty()).collect();
    parts.len() > 1 || (parts.len() == 1 && !parts[0].contains(' '))
}

/// "node /x/node_modules/vite/bin/vite.js --port 5173" -> "vite --port 5173"
pub fn pretty_command(argv: &[String], comm: &str) -> String {
    let mut parts: Vec<String> = argv.iter().map(|a| a.trim().to_string()).filter(|a| !a.is_empty()).collect();
    if parts.is_empty() {
        return comm.into();
    }
    let exe = basename(&parts[0]).to_string();
    if matches!(exe.as_str(), "node" | "bun" | "deno") && parts.len() > 1 {
        let script = parts[1].clone();
        let tool = rules::NODE_TOOLS.iter().find(|t| {
            script.contains(&format!("/{t}/")) || script.contains(&format!("/{t}-cli")) || basename(&script) == **t
        });
        if let Some(t) = tool {
            parts.splice(0..2, [t.to_string()]);
        } else if script.starts_with('/') || script.starts_with('.') {
            parts.splice(0..2, [basename(&script).to_string()]);
        }
    } else {
        parts[0] = exe;
    }
    parts
        .iter()
        .map(|p| if p.starts_with('/') { basename(p) } else { p.as_str() })
        .collect::<Vec<_>>()
        .join(" ")
}

/// host port -> DockerContainer, from the Docker Engine API.
fn docker_port_map() -> Option<HashMap<u16, DockerContainer>> {
    let mut stream = UnixStream::connect("/var/run/docker.sock").ok()?;
    stream.set_read_timeout(Some(Duration::from_millis(1500))).ok()?;
    stream
        .write_all(b"GET /containers/json HTTP/1.0\r\nHost: docker\r\n\r\n")
        .ok()?;
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    let body = response.split_once("\r\n\r\n")?.1;
    let containers: Vec<serde_json::Value> = serde_json::from_str(body).ok()?;
    let mut map = HashMap::new();
    for c in containers {
        let id = c["Id"].as_str().unwrap_or("").to_string();
        let name = c["Names"][0].as_str().unwrap_or("").trim_start_matches('/').to_string();
        let image = c["Image"].as_str().unwrap_or("").to_string();
        for p in c["Ports"].as_array().into_iter().flatten() {
            if let Some(public) = p["PublicPort"].as_u64() {
                map.insert(
                    public as u16,
                    DockerContainer {
                        id: id.clone(),
                        name: name.clone(),
                        image: image.clone(),
                    },
                );
            }
        }
    }
    Some(map)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sockets::Listener;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn pretty_commands() {
        assert_eq!(
            pretty_command(&s(&["/usr/bin/node", "/p/node_modules/vite/bin/vite.js", "--port", "5173"]), "node"),
            "vite --port 5173"
        );
        assert_eq!(pretty_command(&s(&["/usr/bin/python3", "-m", "uvicorn", "app:app"]), "python3"), "python3 -m uvicorn app:app");
        assert_eq!(pretty_command(&s(&["node", "./server.js"]), "node"), "server.js");
        assert_eq!(pretty_command(&[], "next-server"), "next-server");
    }

    #[test]
    fn python_minor_versions_allowed() {
        let allow = |c: &str| c == "python3";
        assert!(is_allowed("python3.12", allow));
        assert!(!is_allowed("python3.x", allow));
    }

    fn proc(pid: i32, ppid: i32, comm: &str) -> Proc {
        Proc { pid, ppid, comm: comm.into(), start_time: 1000.0 + pid as f64, cpu_ticks: 0 }
    }

    /// pnpm -> sh -c -> next dev -> next-server(listening :3000), plus an unrelated
    /// postgres on :5432 under the same shell that must not be absorbed.
    #[test]
    fn groups_wrapper_chain_into_one_server() {
        let me = std::process::id() as i32;
        let base = 900_000;
        let procs: HashMap<i32, Proc> = [
            proc(base + 1, me, "pnpm"),
            proc(base + 2, base + 1, "sh"),
            proc(base + 3, base + 2, "node"),
            proc(base + 4, base + 3, "next-server"),
            proc(base + 5, me, "postgres"),
        ]
        .into_iter()
        .map(|p| (p.pid, p))
        .collect();
        let sockets = SocketScan {
            listening: vec![
                Listener { port: 3000, pid: base + 4, command: "next-server".into(), addresses: vec!["::1".into()] },
                Listener { port: 5432, pid: base + 5, command: "postgres".into(), addresses: vec![] },
            ],
            inbound: HashMap::new(),
            pids_by_port: HashMap::from([(3000, HashSet::from([base + 4])), (5432, HashSet::from([base + 5]))]),
        };
        let prefs = Prefs::default();
        let servers = ScanEngine::default().scan_snapshot(&prefs, &sockets, &procs, 5000.0);
        let web = servers.iter().find(|s| s.port == 3000).expect("next server found");
        assert_eq!(web.root_pid, base + 1, "root climbs through sh -c to pnpm");
        assert_eq!(web.processes.len(), 4);
        assert!(servers.iter().find(|s| s.port == 5432).is_some_and(|s| s.is_protected));
    }
}
