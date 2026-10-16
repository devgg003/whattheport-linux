"""Socket scanner that inspects listening TCP sockets on Linux, including root docker-proxy sockets."""
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, Tuple

@dataclass
class ListeningSocket:
    port: int
    pid: int
    command: str
    addresses: List[str] = field(default_factory=list)

@dataclass
class SocketScan:
    listening: List[ListeningSocket] = field(default_factory=list)
    inbound_connections: Dict[int, int] = field(default_factory=dict)
    listener_pids_by_port: Dict[int, Set[int]] = field(default_factory=dict)

class SocketScanner:
    """Scans listening TCP sockets using ss and /proc cmdline fallback for root docker-proxy processes."""

    @staticmethod
    def scan() -> SocketScan:
        sockets_by_port: Dict[int, Dict[int, ListeningSocket]] = {}
        inbound_conns: Dict[int, int] = {}
        listener_pids: Dict[int, Set[int]] = {}

        docker_proxies = SocketScanner._find_docker_proxy_ports()

        ss_output = SocketScanner._run_ss()
        if ss_output:
            SocketScanner._parse_ss_output(ss_output, sockets_by_port, listener_pids, docker_proxies)

        # Count active inbound connections to listening ports
        inbound_conns = SocketScanner._count_inbound_connections(set(sockets_by_port.keys()))

        # Flatten listening sockets
        all_listening: List[ListeningSocket] = []
        for port, pids_map in sockets_by_port.items():
            for pid, sock in pids_map.items():
                all_listening.append(sock)

        return SocketScan(
            listening=all_listening,
            inbound_connections=inbound_conns,
            listener_pids_by_port=listener_pids
        )

    @staticmethod
    def _find_docker_proxy_ports() -> Dict[int, List[Tuple[str, int]]]:
        """Map host_port -> [('docker-proxy', pid)] by reading world-readable /proc/<pid>/cmdline."""
        port_map: Dict[int, List[Tuple[str, int]]] = {}
        try:
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        raw = f.read()
                    if b"docker-proxy" not in raw:
                        continue
                    args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\x00") if a]
                    if not args or "docker-proxy" not in os.path.basename(args[0]):
                        continue
                    for i, arg in enumerate(args):
                        if arg == "-host-port" and i + 1 < len(args):
                            port = int(args[i + 1])
                            port_map.setdefault(port, []).append(("docker-proxy", pid))
                except Exception:
                    continue
        except Exception:
            pass
        return port_map

    @staticmethod
    def _run_ss() -> Optional[str]:
        try:
            # -l: listening, -t: tcp, -n: numeric, -p: processes, -H: no header
            res = subprocess.run(
                ["ss", "-ltnpH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_ss_output(
        output: str,
        sockets_by_port: Dict[int, Dict[int, ListeningSocket]],
        listener_pids: Dict[int, Set[int]],
        docker_proxies: Dict[int, List[Tuple[str, int]]]
    ):
        for line in output.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue

            local_addr = parts[3]
            port = SocketScanner._extract_port(local_addr)
            if port is None or port <= 0 or port > 65535:
                continue

            users_part = " ".join(parts[4:])
            pids = SocketScanner._extract_pids_from_users(users_part)

            # Fallback to docker-proxy PID if ss hid users:(...) for root-owned docker-proxy
            if not pids and port in docker_proxies:
                pids = docker_proxies[port]

            if not pids:
                continue

            clean_addr = SocketScanner._clean_address(local_addr)
            listener_pids.setdefault(port, set())

            for name, pid in pids:
                listener_pids[port].add(pid)
                if port not in sockets_by_port:
                    sockets_by_port[port] = {}
                if pid not in sockets_by_port[port]:
                    sockets_by_port[port][pid] = ListeningSocket(
                        port=port,
                        pid=pid,
                        command=name,
                        addresses=[clean_addr] if clean_addr else []
                    )
                else:
                    if clean_addr and clean_addr not in sockets_by_port[port][pid].addresses:
                        sockets_by_port[port][pid].addresses.append(clean_addr)

    @staticmethod
    def _extract_port(addr_str: str) -> Optional[int]:
        idx = addr_str.rfind(":")
        if idx != -1:
            try:
                return int(addr_str[idx + 1:])
            except ValueError:
                return None
        return None

    @staticmethod
    def _clean_address(addr_str: str) -> str:
        idx = addr_str.rfind(":")
        if idx != -1:
            addr = addr_str[:idx]
            if addr.startswith("[") and addr.endswith("]"):
                addr = addr[1:-1]
            return addr
        return addr_str

    @staticmethod
    def _extract_pids_from_users(users_str: str) -> List[Tuple[str, int]]:
        results = []
        matches = re.findall(r'\("([^"]+)",pid=(\d+)', users_str)
        names = {m[0] for m in matches}
        # Ignore internal IDE sockets that leak fds to child subprocesses
        if names & {"antigravity", "language_server", "cursor", "code", "claude-desktop"}:
            return []
        my_pid = os.getpid()
        for name, pid_s in matches:
            if name == "ss":
                continue
            try:
                pid = int(pid_s)
                if pid != my_pid:
                    results.append((name, pid))
            except ValueError:
                pass
        return results

    @staticmethod
    def _count_inbound_connections(ports: Set[int]) -> Dict[int, int]:
        counts: Dict[int, int] = {p: 0 for p in ports}
        if not ports:
            return counts
        try:
            res = subprocess.run(
                ["ss", "-tnH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False
            )
            if res.returncode == 0 and res.stdout:
                for line in res.stdout.strip().splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        local_addr = parts[2] if parts[0] in ("ESTAB", "SYN-RECV") else parts[3]
                        p = SocketScanner._extract_port(local_addr)
                        if p in counts:
                            counts[p] += 1
        except Exception:
            pass
        return counts
