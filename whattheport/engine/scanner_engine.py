"""ScanEngine for discovering dev servers, tracking CPU/memory deltas, and hierarchies."""
import os
import subprocess
import time
from typing import Dict, List, Optional, Set, Tuple
from whattheport.engine.agents import AgentSessionResolver
from whattheport.engine.project import ProjectInfo, ProjectResolver
from whattheport.engine.procs import ProcessInspector, ProcSnapshot, ProcArgs
from whattheport.engine.scanner import SocketScanner, SocketScan
from whattheport.engine.server import Server, ServerProcess, Sample
from whattheport.engine.tree import ProcessTree

RUNNERS: Set[str] = {
    "node", "MainThread", "next-server", "vite", "esbuild", "tsx", "ts-node", "workerd", "nest",
    "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "turbo", "nx",
    "python", "python3", "Python", "uv", "poetry", "pipenv", "uvicorn", "gunicorn",
    "ruby", "bundle", "rails", "go", "air", "cargo", "java", "gradle", "mvn",
    "dotnet", "php", "mix", "beam.smp", "elixir",
}

EXCLUDED_DESKTOP_BINARIES: Set[str] = {
    "electron", "cursor", "code", "claude-desktop", "antigravity",
    "language_server", "chrome", "chromium", "firefox", "brave", "grok-bot"
}

SHELLS: Set[str] = {"sh", "bash", "zsh", "dash", "fish"}
AGENT_NAMES: Set[str] = {"claude", "codex", "Conductor"}
AGENT_PATH_MARKERS = ["@anthropic-ai/claude-code", "com.conductor.app", "/codex/"]

class ScanEngine:
    HISTORY_WINDOW = 10 * 60  # 10 minutes

    def __init__(self):
        self.projects = ProjectResolver()
        self.agents = AgentSessionResolver()
        self.previous_cpu: Dict[int, Tuple[float, int, float]] = {}  # pid -> (start_time, cpu_nanos, at_time)
        self.histories: Dict[str, List[Sample]] = {}
        self.last_active: Dict[str, float] = {}
        self.args_cache: Dict[int, Tuple[float, Optional[ProcArgs]]] = {}
        self.docker_cache: Tuple[float, Dict[int, Tuple[str, str]]] = (0.0, {})

    def scan(self, prefs) -> List[Server]:
        now = time.time()
        sockets = SocketScanner.scan()
        processes = ProcessInspector.all_processes()
        return self._scan_snapshot(prefs, sockets, processes, now)

    def _scan_snapshot(self, prefs, sockets: SocketScan, processes: Dict[int, ProcSnapshot], now: float) -> List[Server]:
        ownership = ProcessTree(processes=processes, sockets=sockets)
        sampled: Dict[int, Tuple[int, float]] = {}  # pid -> (mem_bytes, cpu_pct)

        servers: List[Server] = []
        seen_keys: Set[str] = set()
        seen_ports: Set[int] = set()
        live_pids: Set[int] = set()

        min_port = prefs.min_port
        max_port = prefs.max_port
        allowlist = prefs.allowlist
        protected_list = prefs.protected

        for socket in sockets.listening:
            if socket.port < min_port or socket.port > max_port:
                continue
            if socket.port in seen_ports:
                continue
            listener = processes.get(socket.pid)
            if not listener:
                continue
            if not self._is_allowed_listener(socket.command, listener, allowlist):
                continue

            seen_ports.add(socket.port)
            service = ownership.service(listener.pid)
            root = self._root_process(listener, processes, ownership, service)
            tree = ownership.descendants(root.pid, service)
            for p, _ in tree:
                live_pids.add(p.pid)

            total_memory = 0
            total_cpu = 0.0
            nodes: List[ServerProcess] = []
            starts: Dict[int, float] = {}

            for proc, depth in tree:
                if proc.pid not in sampled:
                    usage = ProcessInspector.usage(proc.pid)
                    cpu_pct = 0.0
                    mem_bytes = 0
                    if usage:
                        mem_bytes = usage.footprint
                        if proc.pid in self.previous_cpu:
                            prev_start, prev_nanos, prev_at = self.previous_cpu[proc.pid]
                            if abs(prev_start - proc.start_time) < 1.0 and usage.cpu_nanoseconds >= prev_nanos:
                                elapsed = now - prev_at
                                if elapsed > 0:
                                    cpu_pct = (usage.cpu_nanoseconds - prev_nanos) / (elapsed * 1_000_000_000) * 100.0
                        self.previous_cpu[proc.pid] = (proc.start_time, usage.cpu_nanoseconds, now)
                    else:
                        self.previous_cpu.pop(proc.pid, None)

                    sampled[proc.pid] = (mem_bytes, cpu_pct)

                mem, cpu = sampled[proc.pid]
                total_memory += mem
                total_cpu += cpu
                starts[proc.pid] = proc.start_time
                nodes.append(ServerProcess(
                    pid=proc.pid,
                    name=self._display_name(proc),
                    depth=depth,
                    memory=mem,
                    cpu=cpu
                ))

            root_args = self._get_args(root)
            cwd = ProcessInspector.current_directory(listener.pid) or ProcessInspector.current_directory(root.pid)
            if cwd == "/" and socket.command != "docker-proxy":
                continue

            env = self._inherited_environment(listener, root, processes)
            command_str = self.pretty_command(root_args.arguments, root.comm) if root_args else None

            if socket.command == "docker-proxy":
                docker_info = self._resolve_docker_container(socket.port, now)
                if docker_info:
                    c_name, c_image = docker_info
                    project = ProjectInfo(name=c_name, framework=f"Docker ({c_image})", branch="container")
                    command_str = f"docker: {c_name} ({c_image})"
                else:
                    project = ProjectInfo(name=f"docker:{socket.port}", framework="Docker")
            else:
                project = self.projects.resolve(cwd, command_str)
                if not prefs.get("show_branches", True):
                    project.branch = None

            # Find launcher with intact arguments
            launch_chain = [listener]
            ancestor = listener
            while ancestor.pid != root.pid:
                parent = processes.get(ancestor.ppid)
                if not parent or any(p.pid == parent.pid for p in launch_chain):
                    break
                launch_chain.append(parent)
                ancestor = parent

            launcher = root
            for p in reversed(launch_chain):
                if self._has_intact_args(self._get_args(p)):
                    launcher = p
                    break

            key = f"{socket.port}-{root.pid}-{int(root.start_time)}"
            seen_keys.add(key)
            connections = sockets.inbound_connections.get(socket.port, 0)
            if total_cpu >= 2.0 or connections > 0 or key not in self.last_active:
                self.last_active[key] = now

            history = self.histories.get(key, [])
            history.append(Sample(time=now, memory=total_memory, cpu=total_cpu))
            cutoff = now - self.HISTORY_WINDOW
            history = [s for s in history if s.time >= cutoff]
            self.histories[key] = history

            agent = self.agents.resolve(
                environment=env,
                cwd=cwd,
                link_claude=prefs.get("link_claude", True),
                link_codex=prefs.get("link_codex", True)
            )

            is_protected = socket.command in protected_list or (command_str and any(p in command_str for p in protected_list))

            servers.append(Server(
                port=socket.port,
                pid=listener.pid,
                root_pid=root.pid,
                process_name=socket.command,
                addresses=socket.addresses,
                cwd=cwd,
                cwd_exists=os.path.isdir(cwd) if cwd else True,
                command=command_str,
                launch=self._get_args(launcher),
                launch_directory=ProcessInspector.current_directory(launcher.pid) or cwd,
                started_at=root.start_time,
                project=project,
                agent=agent,
                processes=nodes,
                process_starts=starts,
                memory=total_memory,
                cpu=total_cpu,
                connections=connections,
                history=history,
                last_active=self.last_active.get(key, now),
                is_protected=is_protected
            ))

        # Cleanup expired state
        self.histories = {k: v for k, v in self.histories.items() if k in seen_keys}
        self.last_active = {k: v for k, v in self.last_active.items() if k in seen_keys}
        self.previous_cpu = {k: v for k, v in self.previous_cpu.items() if k in live_pids}
        self.args_cache = {k: v for k, v in self.args_cache.items() if k in processes}

        servers.sort(key=lambda s: s.port)
        return servers

    def _resolve_docker_container(self, port: int, now: float) -> Optional[Tuple[str, str]]:
        cached_at, mapping = self.docker_cache
        if now - cached_at > 10.0:
            new_map: Dict[int, Tuple[str, str]] = {}
            try:
                res = subprocess.run(
                    ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Ports}}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=1.5,
                    check=False
                )
                if res.returncode == 0 and res.stdout:
                    for line in res.stdout.strip().splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            name, image, ports_str = parts[0], parts[1], parts[2]
                            for token in ports_str.split(","):
                                if "->" in token:
                                    host_part = token.split("->")[0].strip()
                                    colon = host_part.rfind(":")
                                    if colon != -1:
                                        try:
                                            p = int(host_part[colon + 1:])
                                            new_map[p] = (name, image)
                                        except ValueError:
                                            pass
            except Exception:
                pass
            self.docker_cache = (now, new_map)
            mapping = new_map
        return mapping.get(port)

    def _root_process(self, listener: ProcSnapshot, processes: Dict[int, ProcSnapshot], ownership: ProcessTree, service: Set[int]) -> ProcSnapshot:
        current = listener
        visited: Set[int] = {listener.pid}
        while True:
            if current.ppid <= 1:
                break
            parent = processes.get(current.ppid)
            if not parent or parent.pid in visited:
                break
            visited.add(parent.pid)
            if ownership.contains_other_service(parent.pid, service):
                break

            if self._is_proc_allowed(parent, RUNNERS) and not self._is_agent(parent):
                current = parent
                continue

            # Stepping over package manager shell sh -c
            if parent.comm in SHELLS and parent.ppid > 1:
                grandparent = processes.get(parent.ppid)
                if grandparent and self._is_proc_allowed(grandparent, RUNNERS) and not self._is_agent(grandparent) and grandparent.pid not in visited:
                    visited.add(grandparent.pid)
                    if ownership.contains_other_service(grandparent.pid, service):
                        current = parent
                        break
                    current = grandparent
                    continue
            break
        return current

    def _is_agent(self, proc: ProcSnapshot) -> bool:
        if proc.comm in AGENT_NAMES:
            return True
        args = self._get_args(proc)
        if not args:
            return False
        joined = " ".join([args.executable_path] + args.arguments[:3])
        return any(marker in joined for marker in AGENT_PATH_MARKERS)

    def _is_allowed_listener(self, socket_command: str, listener: ProcSnapshot, allowlist: Set[str]) -> bool:
        # Exclude desktop Electron/IDE processes
        args = self._get_args(listener)
        if args:
            exe_base = os.path.basename(args.executable_path) if args.executable_path else ""
            arg0_base = os.path.basename(args.arguments[0]) if args.arguments else ""
            if exe_base in EXCLUDED_DESKTOP_BINARIES or arg0_base in EXCLUDED_DESKTOP_BINARIES or socket_command in EXCLUDED_DESKTOP_BINARIES:
                return False

        if self._is_allowed(socket_command, allowlist):
            return True
        return self._is_proc_allowed(listener, allowlist)

    def _is_proc_allowed(self, proc: ProcSnapshot, allowlist: Set[str]) -> bool:
        if self._is_allowed(proc.comm, allowlist):
            return True
        args = self._get_args(proc)
        if not args:
            return False
        if args.executable_path:
            exe_base = os.path.basename(args.executable_path)
            if exe_base in EXCLUDED_DESKTOP_BINARIES:
                return False
            if self._is_allowed(exe_base, allowlist):
                return True
        if args.arguments:
            arg0_base = os.path.basename(args.arguments[0].split()[0])
            if arg0_base in EXCLUDED_DESKTOP_BINARIES:
                return False
            if self._is_allowed(arg0_base, allowlist):
                return True
        return False

    def _is_allowed(self, command: str, allowlist: Set[str]) -> bool:
        if not command:
            return False
        if command in allowlist:
            return True
        if "python3" in allowlist and command.startswith("python3."):
            ver = command[len("python3."):]
            return ver.replace(".", "").isdigit()
        return False

    def _inherited_environment(self, listener: ProcSnapshot, root: ProcSnapshot, processes: Dict[int, ProcSnapshot]) -> Dict[str, str]:
        chain = [listener]
        curr = listener
        extra_hops = 2
        while curr.ppid > 1 and len(chain) < 10:
            parent = processes.get(curr.ppid)
            if not parent:
                break
            if curr.pid == root.pid or any(p.pid == root.pid for p in chain):
                if extra_hops <= 0:
                    break
                extra_hops -= 1
            chain.append(parent)
            curr = parent

        env: Dict[str, str] = {}
        for p in reversed(chain):
            pargs = self._get_args(p)
            if pargs:
                env.update(pargs.environment)
        return env

    def _has_intact_args(self, args: Optional[ProcArgs]) -> bool:
        if not args:
            return False
        parts = [a.strip() for a in args.arguments if a.strip()]
        return len(parts) > 1 or (len(parts) == 1 and " " not in parts[0])

    def _get_args(self, proc: ProcSnapshot) -> Optional[ProcArgs]:
        if proc.pid in self.args_cache:
            start_t, cached_args = self.args_cache[proc.pid]
            if abs(start_t - proc.start_time) < 1.0:
                return cached_args
        args = ProcessInspector.arguments(proc.pid)
        self.args_cache[proc.pid] = (proc.start_time, args)
        return args

    def _display_name(self, proc: ProcSnapshot) -> str:
        args = self._get_args(proc)
        if not args or not args.arguments:
            return proc.comm
        cmd = self.pretty_command(args.arguments, proc.comm)
        paren_idx = cmd.find(" (")
        if paren_idx != -1:
            return cmd[:paren_idx]
        return cmd

    @staticmethod
    def pretty_command(arguments: List[str], comm: str) -> str:
        clean = [a.strip() for a in arguments if a.strip()]
        if not clean:
            return comm
        parts = list(clean)
        first = parts[0]
        exe = os.path.basename(first)

        if exe in ("node", "bun", "deno") and len(parts) > 1:
            script = parts[1]
            tools = ["npm", "npx", "pnpm", "yarn", "vite", "next", "astro", "nuxt", "storybook", "tsx", "turbo", "playwright", "jest"]
            matched_tool = next((t for t in tools if f"/{t}/" in script or f"/{t}-cli" in script or os.path.basename(script) == t), None)
            if matched_tool:
                parts = [matched_tool] + parts[2:]
            elif script.startswith("/") or script.startswith("."):
                parts = [os.path.basename(script)] + parts[2:]
        else:
            parts[0] = exe

        return " ".join(os.path.basename(p) if p.startswith("/") else p for p in parts)
