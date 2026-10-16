"""Process tree analysis and ownership resolution matching WhatThePort's ProcessTree."""
from typing import Dict, List, Set, Tuple
from whattheport.engine.procs import ProcSnapshot
from whattheport.engine.scanner import SocketScan

class ProcessTree:
    def __init__(self, processes: Dict[int, ProcSnapshot], sockets: SocketScan):
        self.processes = processes
        self.children: Dict[int, List[int]] = {}
        for p in processes.values():
            self.children.setdefault(p.ppid, []).append(p.pid)

        self.listeners_by_port = dict(sockets.listener_pids_by_port)
        for s in sockets.listening:
            self.listeners_by_port.setdefault(s.port, set()).add(s.pid)

        self.ports_by_pid: Dict[int, Set[int]] = {}
        for port, pids in self.listeners_by_port.items():
            for pid in pids:
                self.ports_by_pid.setdefault(pid, set()).add(port)

    def service(self, pid: int) -> Set[int]:
        """Reloaders/workers sharing a socket belong to the same service.
        Follow shared sockets transitively."""
        result: Set[int] = {pid}
        pending: List[int] = [pid]
        while pending:
            curr = pending.pop()
            for port in self.ports_by_pid.get(curr, set()):
                for peer in self.listeners_by_port.get(port, set()):
                    if peer not in result:
                        result.add(peer)
                        pending.append(peer)
        return result

    def contains_other_service(self, pid: int, service: Set[int]) -> bool:
        pending: List[int] = [pid]
        seen: Set[int] = set()
        while pending:
            curr = pending.pop()
            if curr in seen:
                continue
            seen.add(curr)
            if curr in self.ports_by_pid and curr not in service:
                return True
            pending.extend(self.children.get(curr, []))
        return False

    def descendants(self, root: int, service: Set[int]) -> List[Tuple[ProcSnapshot, int]]:
        result: List[Tuple[ProcSnapshot, int]] = []
        seen: Set[int] = set()

        def visit(pid: int, depth: int):
            if pid in seen:
                return
            seen.add(pid)
            proc = self.processes.get(pid)
            if not proc:
                return
            result.append((proc, depth))
            for child_pid in sorted(self.children.get(pid, [])):
                if not self.contains_other_service(child_pid, service):
                    visit(child_pid, depth + 1)

        visit(root, 0)
        return result
