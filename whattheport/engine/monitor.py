"""Monitor orchestrating scans, alert evaluation, and clean-up recommendations."""
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple
from whattheport.engine.format import duration, bytes_str
from whattheport.engine.github import GitHubLookup
from whattheport.engine.prefs import Preferences
from whattheport.engine.procs import ProcessInspector
from whattheport.engine.scanner_engine import ScanEngine
from whattheport.engine.server import CleanUpReason, Server, ServerStatus

class ServerMonitor:
    def __init__(self, prefs: Preferences):
        self.prefs = prefs
        self.engine = ScanEngine()
        self.github = GitHubLookup()
        self.servers: List[Server] = []
        self.port_color_indices: Dict[int, int] = {}
        self.system_memory: Tuple[int, int, int] = (0, 0, 0)  # total, used, avail
        self.system_cpu: float = 0.0
        self.prev_sys_cpu: Optional[Tuple[int, int]] = None  # (idle, total)
        self.is_scanning = False
        self.listeners: List[Callable[[], None]] = []
        self.handled_cleanups: Set[str] = set()
        self.snoozed_alerts: Dict[str, float] = {}  # server_key -> snooze_until_timestamp
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def add_listener(self, callback: Callable[[], None]):
        self.listeners.append(callback)

    def notify_listeners(self):
        for cb in self.listeners:
            try:
                cb()
            except Exception:
                pass

    def start(self):
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self):
        # Run first scan immediately
        self.scan()
        while not self._stop_event.is_set():
            interval = self.prefs.scan_interval
            self._stop_event.wait(max(interval, 1.0))
            if not self._stop_event.is_set():
                self.scan()

    def scan(self):
        if self.is_scanning:
            return
        self.is_scanning = True
        try:
            res = self.engine.scan(self.prefs)
            sys_mem = ProcessInspector.system_memory()
            sys_cpu = self._sample_system_cpu()

            # Maintain stable color indices by port
            for s in sorted(res, key=lambda x: x.port):
                if s.port not in self.port_color_indices:
                    self.port_color_indices[s.port] = len(self.port_color_indices)

            self.servers = res
            self.system_memory = sys_mem
            self.system_cpu = sys_cpu

            # Check alerts & cleanup
            self._evaluate_alerts(res)
            self._handle_cleanup(res)

            # GitHub refresh in background if enabled
            if self.prefs.get("vercel_previews") or self.prefs.get("github_pull_requests"):
                for s in res:
                    if s.project and s.project.root and s.project.branch:
                        self.github.refresh(
                            s.project.root,
                            s.project.branch,
                            self.prefs.get("vercel_previews", False),
                            self.prefs.get("github_pull_requests", False)
                        )

            self.notify_listeners()
        finally:
            self.is_scanning = False

    def color_index(self, port: int) -> int:
        return self.port_color_indices.get(port, 0)

    @property
    def total_server_memory(self) -> int:
        unique_pids: Dict[int, int] = {}
        for s in self.servers:
            for p in s.processes:
                unique_pids[p.pid] = p.memory
        return sum(unique_pids.values())

    @property
    def total_server_cpu(self) -> float:
        unique_pids: Dict[int, float] = {}
        for s in self.servers:
            for p in s.processes:
                unique_pids[p.pid] = p.cpu
        return sum(unique_pids.values())

    @property
    def needs_attention(self) -> bool:
        thresh = self.prefs.threshold_bytes
        return any(s.status(thresh) == ServerStatus.ATTENTION for s in self.servers)

    def cleanup_reason(self, server: Server) -> Optional[CleanUpReason]:
        if server.is_protected or self.prefs.cleanup_mode == "off":
            return None
        if not server.cwd_exists:
            if self.prefs.get("cleanup_deleted_worktree", True):
                return CleanUpReason("worktreeDeleted")
            return None
        if server.idle_for >= self.prefs.idle_threshold_seconds:
            return CleanUpReason("idle", duration(server.idle_for))
        if server.uptime >= self.prefs.running_threshold_seconds:
            return CleanUpReason("longRunning", duration(server.uptime))
        if server.is_leaking():
            return CleanUpReason("leaking", bytes_str(server.memory_growth))
        return None

    @property
    def cleanup_candidates(self) -> List[Tuple[Server, CleanUpReason]]:
        results = []
        for s in self.servers:
            r = self.cleanup_reason(s)
            if r:
                results.append((s, r))
        return results

    @property
    def suggested_cleanup_count(self) -> int:
        return len([c for c in self.cleanup_candidates if not c[1].is_leak])

    def _sample_system_cpu(self) -> float:
        try:
            with open("/proc/stat", "r") as f:
                first_line = f.readline()
            parts = [float(x) for x in first_line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            total = sum(parts)
            cpu = 0.0
            if self.prev_sys_cpu:
                prev_idle, prev_total = self.prev_sys_cpu
                idle_delta = idle - prev_idle
                total_delta = total - prev_total
                if total_delta > 0:
                    cpu = (1.0 - (idle_delta / total_delta)) * 100.0
            self.prev_sys_cpu = (idle, total)
            return max(0.0, min(100.0, cpu))
        except Exception:
            return 0.0

    def _evaluate_alerts(self, servers: List[Server]):
        if not self.prefs.get("leak_warnings", True):
            return
        now = time.time()
        thresh = self.prefs.threshold_bytes
        for s in servers:
            key = f"{s.port}-{s.root_pid}"
            if s.status(thresh) == ServerStatus.ATTENTION:
                if key in self.snoozed_alerts and now < self.snoozed_alerts[key]:
                    continue
                # Trigger notification
                self.snoozed_alerts[key] = now + (self.prefs.get("snooze_minutes", 60) * 60)
                self._send_notification(
                    title=f"WhatThePort: Port {s.port} High Memory",
                    body=f"{s.display_name} is using {bytes_str(s.memory)} (threshold {bytes_str(thresh)})",
                    urgency="critical"
                )

    def _handle_cleanup(self, servers: List[Server]):
        mode = self.prefs.cleanup_mode
        if mode == "off":
            return
        candidates = [c for c in self.cleanup_candidates if not c[1].is_leak]
        now_keys = {f"{c[0].port}-{c[0].root_pid}" for c in candidates}
        fresh = [c for c in candidates if f"{c[0].port}-{c[0].root_pid}" not in self.handled_cleanups]

        self.handled_cleanups.intersection_update(now_keys)
        if not fresh:
            return

        for c in fresh:
            self.handled_cleanups.add(f"{c[0].port}-{c[0].root_pid}")

        notify = self.prefs.get("cleanup_notify", True)
        if mode == "automatic":
            from whattheport.engine.control import ProcessControl
            for c in fresh:
                ProcessControl.stop(c[0])
            if notify:
                names = ", ".join(f":{c[0].port}" for c in fresh)
                self._send_notification("WhatThePort Clean Up", f"Automatically stopped idle server(s): {names}")
        elif mode == "ask" and notify:
            names = ", ".join(f":{c[0].port}" for c in fresh)
            self._send_notification("WhatThePort Clean Up", f"Server(s) ready for cleanup: {names}")

    def _send_notification(self, title: str, body: str, urgency: str = "normal"):
        try:
            subprocess.run(
                ["notify-send", "-a", "WhatThePort", "-u", urgency, title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
        except Exception:
            pass
