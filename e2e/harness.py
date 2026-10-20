"""Process, D-Bus and filesystem helpers shared by the WhatThePort end-to-end tests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).resolve().parent / "assets"
WTP = ROOT / "target" / "release" / "wtp"

# Ports sit in a private range so unrelated listeners on the machine are ignored.
PORT_CLAUDE = 18010
PORT_CODEX_EXACT = 18020
PORT_CODEX_ESTIMATED = 18021
PORT_STOP = 18030
PORT_DELETED = 18040
PORT_GROW = 18050
PORT_POSTGRES = 18060
PORT_MISSING = 18099

CLAUDE_ID = "claude-session-e2e-oauth"
CLAUDE_TITLE = "Fix OAuth callback"
CODEX_TITLE_A = "Wire up the billing webhook"
CODEX_TITLE_B = "Migrate docs to Astro 5"
THREAD_A = "thread-a-billing"
THREAD_B = "thread-b-docs"


class CheckFailure(AssertionError):
    pass


class Report:
    """Collect every check so one failure does not hide the rest."""

    def __init__(self):
        self.results: list[dict] = []
        self.notes: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append({"name": name, "ok": ok, "detail": detail})
        status = "PASS" if ok else "FAIL"
        line = f"{status}  {name}"
        if detail and not ok:
            line += f"\n      {detail}"
        print(line, flush=True)
        return ok

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"NOTE  {text}", flush=True)

    def finish(self, path: Path | None = None) -> int:
        failed = [r for r in self.results if not r["ok"]]
        summary = {
            "passed": len(self.results) - len(failed),
            "failed": len(failed),
            "notes": self.notes,
            "results": self.results,
        }
        print(
            f"\n{summary['passed']} passed, {summary['failed']} failed, {len(self.notes)} notes",
            flush=True,
        )
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return 1 if failed else 0


def terminate(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pidfile(path: Path, timeout: float = 8.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        time.sleep(0.1)
    raise CheckFailure(f"pidfile {path} was not written")


def wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def port_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        return True


def wait_until(predicate, timeout: float, interval: float = 0.4, desc: str = "condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise CheckFailure(f"timed out after {timeout:.0f}s waiting for {desc}; last={last!r}")


class Session:
    """Isolated HOME, config, session bus and PATH for one wtp daemon."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.runtime = root / "runtime"
        self.logs = root / "logs"
        self.projects = self.home / "projects"
        self.open_log = root / "xdg-open.log"
        self.notify_log = root / "notifications.jsonl"
        self.daemon_log = self.logs / "daemon.log"
        self.children: list[subprocess.Popen] = []
        self.server_pids: list[int] = []
        self.bus: subprocess.Popen | None = None
        self.daemon: subprocess.Popen | None = None
        self.env = os.environ.copy()
        # Never let the test runner's own agent variables leak into servers via
        # ancestor /proc environ. Servers are reparented to init, but be explicit.
        for key in list(self.env):
            if key.startswith("CLAUDE_") or key.startswith("CODEX_") or key in {"WTP_DAEMONIZED"}:
                del self.env[key]

    def start_bus(self) -> None:
        self.home.mkdir(parents=True)
        self.runtime.mkdir(mode=0o700)
        self.logs.mkdir()
        self.projects.mkdir(parents=True)
        bus_path = self.runtime / "bus"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        xdg = bin_dir / "xdg-open"
        shutil.copy(ASSETS / "xdg-open", xdg)
        xdg.chmod(0o755)
        # Environment is applied before the bus starts so dbus-activated
        # services (xfconf, at-spi) inherit the isolated HOME and display.
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus_path}",
                "PATH": f"{bin_dir}:{self.env.get('PATH', '')}",
                "WTP_OPEN_LOG": str(self.open_log),
                # CLI tests have no display. UI tests set WTP_KEEP_DISPLAY.
                "DISPLAY": self.env.get("WTP_KEEP_DISPLAY", ""),
            }
        )
        if not self.env["DISPLAY"]:
            self.env.pop("DISPLAY", None)
        self._write_config()
        self.bus = subprocess.Popen(
            [
                "dbus-daemon",
                "--session",
                "--nofork",
                "--nopidfile",
                "--address",
                f"unix:path={bus_path}",
            ],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.children.append(self.bus)
        deadline = time.time() + 5
        while not bus_path.exists():
            if time.time() > deadline:
                raise CheckFailure("session bus socket did not appear")
            time.sleep(0.05)

    def _write_config(self) -> None:
        config = {
            "min_port": 18000,
            "max_port": 18100,
            "scan_interval": 1.0,
            "threshold_gb": 2.0,
            "leak_warnings": True,
            "snooze_minutes": 60,
            "cleanup_mode": "ask",
            "cleanup_notify": True,
            "cleanup_deleted_worktree": True,
            "cleanup_idle_hours": 4,
            "cleanup_running_days": 3,
            "force_quit_seconds": 1.0,
            "link_claude": True,
            "link_codex": True,
            "show_branches": True,
            "github_pull_requests": False,
            "vercel_previews": False,
            "tray": "off",
        }
        path = self.home / ".config" / "whattheport" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def write_agent_fixtures(self, codex_cwd: Path) -> None:
        projects = self.home / ".claude" / "projects" / "e2e"
        projects.mkdir(parents=True)
        # Claude Code writes compact JSONL. The scanner matches the literal
        # `"type":"custom-title"` (no space after the colon).
        (projects / f"{CLAUDE_ID}.jsonl").write_text(
            json.dumps(
                {
                    "type": "custom-title",
                    "customTitle": CLAUDE_TITLE,
                    "sessionId": CLAUDE_ID,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        codex = self.home / ".codex"
        sessions = codex / "sessions" / "2026" / "10" / "19"
        sessions.mkdir(parents=True)
        (codex / "session_index.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": THREAD_A,
                            "thread_name": CODEX_TITLE_A,
                            "updated_at": "2026-10-19T12:05:00Z",
                        }
                    ),
                    json.dumps(
                        {
                            "id": THREAD_B,
                            "thread_name": CODEX_TITLE_B,
                            "updated_at": "2026-10-19T13:05:00Z",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        cwd = str(codex_cwd)
        for thread, stamp in (
            (THREAD_A, "2026-10-19T12:00:00.000Z"),
            (THREAD_B, "2026-10-19T13:00:00.000Z"),
        ):
            name = f"rollout-{stamp.replace(':', '-')[:19]}-{thread}.jsonl"
            payload = {
                "timestamp": stamp,
                "type": "session_meta",
                "payload": {
                    "id": thread,
                    "session_id": thread,
                    "cwd": cwd,
                    "timestamp": stamp,
                },
            }
            (sessions / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def git_init(self, path: Path) -> None:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            env={**self.env, "GIT_AUTHOR_NAME": "wtp", "GIT_AUTHOR_EMAIL": "wtp@example.com"},
        )

    def start_notifyd(self) -> None:
        proc = subprocess.Popen(
            ["python3", str(Path(__file__).resolve().parent / "notifyd.py"), str(self.notify_log)],
            env=self.env,
            stdout=self._log("notifyd.log"),
            stderr=subprocess.STDOUT,
        )
        self.children.append(proc)
        # Give the name owner a moment to register before the daemon scans.
        time.sleep(0.4)

    def notifications(self) -> list[dict]:
        if not self.notify_log.exists():
            return []
        rows = []
        for line in self.notify_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "summary" in row:
                rows.append(row)
        return rows

    def _log(self, name: str):
        return open(self.logs / name, "ab", buffering=0)

    def spawn_node(self, project: Path, port: int, extra_env: dict | None = None) -> int:
        project.mkdir(parents=True, exist_ok=True)
        script = project / "server.js"
        if not script.exists():
            shutil.copy(ASSETS / "node_server.js", script)
        pidfile = self.logs / f"node-{port}.pid"
        env = self.env.copy()
        env.update({"PORT": str(port), "PIDFILE": str(pidfile)})
        if extra_env:
            env.update(extra_env)
        subprocess.run(
            ["node", str(script)],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log(f"node-{port}.log"),
            stderr=subprocess.STDOUT,
            timeout=10,
            check=True,
        )
        pid = read_pidfile(pidfile)
        self.server_pids.append(pid)
        if not wait_port(port):
            raise CheckFailure(f"node server on :{port} never accepted connections")
        return pid

    def spawn_python(self, project: Path, port: int, extra_env: dict | None = None) -> int:
        project.mkdir(parents=True, exist_ok=True)
        script = project / "py_server.py"
        if not script.exists():
            shutil.copy(ASSETS / "py_server.py", script)
        pidfile = self.logs / f"py-{port}.pid"
        env = self.env.copy()
        env.update({"PORT": str(port), "PIDFILE": str(pidfile)})
        if extra_env:
            env.update(extra_env)
        subprocess.run(
            ["python3", str(script)],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log(f"py-{port}.log"),
            stderr=subprocess.STDOUT,
            timeout=10,
            check=True,
        )
        pid = read_pidfile(pidfile)
        self.server_pids.append(pid)
        if not wait_port(port):
            raise CheckFailure(f"python server on :{port} never accepted connections")
        return pid

    def spawn_postgres(self, project: Path, port: int) -> int:
        project.mkdir(parents=True, exist_ok=True)
        binary = project / "postgres"
        subprocess.run(
            ["gcc", "-O2", "-o", str(binary), str(ASSETS / "postgres.c")],
            check=True,
            capture_output=True,
            text=True,
        )
        pidfile = self.logs / f"postgres-{port}.pid"
        env = self.env.copy()
        env.update({"PORT": str(port), "PIDFILE": str(pidfile)})
        subprocess.run(
            [str(binary)],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log(f"postgres-{port}.log"),
            stderr=subprocess.STDOUT,
            timeout=10,
            check=True,
        )
        pid = read_pidfile(pidfile)
        self.server_pids.append(pid)
        if not wait_port(port):
            raise CheckFailure(f"postgres stand-in on :{port} never accepted connections")
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        if comm != "postgres":
            raise CheckFailure(f"expected comm postgres, got {comm!r}")
        return pid

    def start_daemon(self, tray: bool = False) -> None:
        if not WTP.exists():
            raise CheckFailure(f"missing {WTP}; build with cargo build --release --locked")
        args = [str(WTP), "daemon"] + (["--tray"] if tray else [])
        log = open(self.daemon_log, "ab", buffering=0)
        self.daemon = subprocess.Popen(
            args,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.children.append(self.daemon)

    def daemon_log_text(self) -> str:
        if not self.daemon_log.exists():
            return ""
        return self.daemon_log.read_text(encoding="utf-8", errors="replace")

    def run(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(WTP), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def json_servers(self) -> list[dict]:
        proc = self.run("--json")
        if proc.returncode != 0:
            raise CheckFailure(f"wtp --json failed: {proc.stderr}")
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            raise CheckFailure(f"wtp --json did not print a list: {proc.stdout[:400]}")
        return data

    def server(self, port: int) -> dict | None:
        for row in self.json_servers():
            if row.get("port") == port:
                return row
        return None

    def close(self) -> None:
        if self.daemon and pid_alive(self.daemon.pid):
            try:
                self.run("quit", timeout=5)
            except Exception:
                pass
            time.sleep(0.4)
            if pid_alive(self.daemon.pid):
                terminate(self.daemon.pid, signal.SIGTERM)
        for pid in self.server_pids:
            if pid_alive(pid):
                terminate(pid, signal.SIGKILL)
        for proc in self.children:
            if proc.poll() is None:
                terminate(proc.pid, signal.SIGTERM)
        time.sleep(0.2)
        for proc in self.children:
            if proc.poll() is None:
                terminate(proc.pid, signal.SIGKILL)
