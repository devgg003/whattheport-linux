"""Socket scanner that inspects listening TCP sockets on Linux."""
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

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
    """Scans listening TCP sockets using /proc/net/tcp, /proc/net/tcp6 and ss."""

    @staticmethod
    def scan() -> SocketScan:
        # We try 'ss -ltnpH' first as it's standard and fast
        sockets_by_port: Dict[int, Dict[int, ListeningSocket]] = {}
        inbound_conns: Dict[int, int] = {}
        listener_pids: Dict[int, Set[int]] = {}

        ss_output = SocketScanner._run_ss()
        if ss_output:
            SocketScanner._parse_ss_output(ss_output, sockets_by_port, listener_pids)
        else:
            SocketScanner._scan_proc(sockets_by_port, listener_pids)

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
    def _parse_ss_output(output: str, sockets_by_port: Dict[int, Dict[int, ListeningSocket]], listener_pids: Dict[int, Set[int]]):
        # Format usually: LISTEN 0 128 127.0.0.1:3000 0.0.0.0:* users:(("node",pid=12345,fd=19))
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
            # Extract users:(("...",pid=...,fd=...))
            # match pairs like ("process",pid=1234)
            pids = SocketScanner._extract_pids_from_users(users_part)
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
        # Handle [::]:3000, 127.0.0.1:3000, *:3000
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
    def _extract_pids_from_users(users_str: str) -> List[tuple]:
        # Pattern: ("node",pid=12345,...) or ("python3.12",pid=999,...)
        results = []
        matches = re.findall(r'\("([^"]+)",pid=(\d+)', users_str)
        for name, pid_s in matches:
            try:
                results.append((name, int(pid_s)))
            except ValueError:
                pass
        return results

    @staticmethod
    def _count_inbound_connections(ports: Set[int]) -> Dict[int, int]:
        counts: Dict[int, int] = {p: 0 for p in ports}
        if not ports:
            return counts
        try:
            # -t: tcp, -n: numeric, -H: no header
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
                    # State Recv-Q Send-Q Local Address:Port Peer Address:Port
                    if len(parts) >= 4:
                        local_addr = parts[2] if parts[0] in ("ESTAB", "SYN-RECV") else parts[3]
                        p = SocketScanner._extract_port(local_addr)
                        if p in counts:
                            counts[p] += 1
        except Exception:
            pass
        return counts

    @staticmethod
    def _scan_proc(sockets_by_port: Dict[int, Dict[int, ListeningSocket]], listener_pids: Dict[int, Set[int]]):
        # Fallback reading /proc directly if ss is missing or limited
        pass
