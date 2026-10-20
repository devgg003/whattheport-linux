#!/usr/bin/env python3
"""End-to-end checks for the wtp CLI and the D-Bus daemon.

Starts real Node and Python listeners, a process whose comm is `postgres`,
fake Claude Code and Codex sessions, and a notification service. The daemon
under test is the release binary; nothing is mocked inside the scanner.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

from harness import (
    CLAUDE_TITLE,
    CODEX_TITLE_A,
    CODEX_TITLE_B,
    PORT_CLAUDE,
    PORT_CODEX_ESTIMATED,
    PORT_CODEX_EXACT,
    PORT_DELETED,
    PORT_GROW,
    PORT_MISSING,
    PORT_POSTGRES,
    PORT_STOP,
    THREAD_A,
    Report,
    Session,
    pid_alive,
    port_closed,
    wait_port,
    wait_until,
)

ROOT = Path(__file__).resolve().parents[1]


def by_port(servers: list[dict], port: int) -> dict:
    for row in servers:
        if row.get("port") == port:
            return row
    raise KeyError(port)


def main() -> int:
    report = Report()
    tmp = Path(tempfile.mkdtemp(prefix="wtp-e2e-"))
    session = Session(tmp)
    out_dir = ROOT / "e2e" / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _run(report, session, out_dir)
    except Exception as exc:
        report.check("suite did not crash", False, f"{type(exc).__name__}: {exc}")
        log = session.daemon_log_text()
        if log:
            (out_dir / "daemon-cli.log").write_text(log, encoding="utf-8")
            report.note(f"daemon log tail: {log[-1500:]}")
    finally:
        session.close()
        # Keep the temp tree only when something failed, so a bug can be reproduced.
        if any(not r["ok"] for r in report.results):
            kept = out_dir / "failed-home"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(session.home, kept, dirs_exist_ok=True)
            report.note(f"preserved home at {kept}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return report.finish(out_dir / "cli-daemon-report.json")


def _run(report: Report, session: Session, out_dir: Path) -> None:
    session.start_bus()
    codex_dir = session.projects / "codex-app"
    claude_dir = session.projects / "web-dashboard"
    vite_dir = session.projects / "studio-vite"
    deleted_dir = session.projects / "billing-api-invoices"
    grow_dir = session.projects / "leak-lab"
    db_dir = session.projects / "local-db"

    for path, manifest in (
        (
            claude_dir,
            {"name": "web-dashboard", "dependencies": {"next": "15.2.0"}},
        ),
        (
            vite_dir,
            {"name": "studio-vite", "dependencies": {"vite": "6.1.0"}},
        ),
    ):
        path.mkdir(parents=True)
        (path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        session.git_init(path)

    codex_dir.mkdir(parents=True)
    (codex_dir / "pyproject.toml").write_text(
        '[project]\nname = "billing-api"\ndependencies = ["fastapi"]\n',
        encoding="utf-8",
    )
    session.git_init(codex_dir)
    session.write_agent_fixtures(codex_dir)

    deleted_dir.mkdir()
    (deleted_dir / "package.json").write_text(
        json.dumps({"name": "billing-api-invoices"}),
        encoding="utf-8",
    )
    grow_dir.mkdir()
    (grow_dir / "pyproject.toml").write_text('[project]\nname = "leak-lab"\n', encoding="utf-8")
    db_dir.mkdir()
    (db_dir / "package.json").write_text(json.dumps({"name": "local-db"}), encoding="utf-8")

    session.start_notifyd()

    claude_pid = session.spawn_node(
        claude_dir,
        PORT_CLAUDE,
        {"CLAUDE_CODE_SESSION_ID": "claude-session-e2e-oauth"},
    )
    exact_pid = session.spawn_python(
        codex_dir,
        PORT_CODEX_EXACT,
        {"CODEX_THREAD_ID": THREAD_A},
    )
    estimated_pid = session.spawn_python(codex_dir, PORT_CODEX_ESTIMATED, {})
    stop_pid = session.spawn_node(vite_dir, PORT_STOP, {})
    deleted_pid = session.spawn_python(deleted_dir, PORT_DELETED, {})
    grow_pid = session.spawn_python(grow_dir, PORT_GROW, {})
    postgres_pid = session.spawn_postgres(db_dir, PORT_POSTGRES)
    report.check("real listeners accepted connections", True, f"pids={[claude_pid, exact_pid, estimated_pid, stop_pid, deleted_pid, grow_pid, postgres_pid]}")

    session.start_daemon(tray=False)
    try:
        servers = wait_until(
            lambda: _ready(session),
            timeout=20,
            desc="all seven servers in wtp --json",
        )
    except Exception as exc:
        report.check("daemon lists every dev server", False, f"{exc}\n{session.daemon_log_text()[-2000:]}")
        return
    report.check("daemon lists every dev server", True)
    (out_dir / "list.json").write_text(json.dumps(servers, indent=2) + "\n", encoding="utf-8")

    text = session.run()
    report.check("wtp list exits 0", text.returncode == 0, text.stderr)
    report.check("list output is plain text when stdout is not a tty", "\x1b" not in text.stdout, repr(text.stdout[:200]))
    report.check("list summary is present", "in servers" in text.stdout and "free" in text.stdout, text.stdout)
    (out_dir / "list.txt").write_text(text.stdout, encoding="utf-8")

    help_text = session.run("--help")
    report.check(
        "wtp --help describes --json as a JSON array of servers",
        help_text.returncode == 0
        and "Print the server list as a JSON array" in help_text.stdout
        and "full state" not in help_text.stdout,
        help_text.stdout,
    )

    raw_json = session.run("--json")
    parsed = json.loads(raw_json.stdout)
    report.check(
        "wtp --json prints the server list",
        raw_json.returncode == 0 and isinstance(parsed, list),
        raw_json.stdout[:300],
    )
    report.check(
        "wtp list --json is the same list",
        session.run("list", "--json").stdout == raw_json.stdout,
    )

    claude = by_port(servers, PORT_CLAUDE)
    exact = by_port(servers, PORT_CODEX_EXACT)
    guessed = by_port(servers, PORT_CODEX_ESTIMATED)
    deleted = by_port(servers, PORT_DELETED)
    grow = by_port(servers, PORT_GROW)
    protected = by_port(servers, PORT_POSTGRES)
    vite = by_port(servers, PORT_STOP)

    report.check("node server is named from package.json", claude["name"] == "web-dashboard", json.dumps(claude))
    report.check("node framework is Next.js 15", claude.get("framework") == "Next.js 15", str(claude.get("framework")))
    report.check("node branch is main", claude.get("branch") == "main", str(claude.get("branch")))
    report.check(
        "Claude Code session is linked exactly",
        claude.get("session_estimated") is False
        and claude.get("agent") == f"Claude Code: {CLAUDE_TITLE}"
        and claude["subtitle"].startswith(CLAUDE_TITLE)
        and not claude["subtitle"].startswith("~"),
        json.dumps({k: claude.get(k) for k in ("agent", "subtitle", "session_estimated")}),
    )
    report.check("claude server can be restarted", claude.get("can_restart") is True and claude.get("protected") is False)

    report.check("python project name comes from pyproject.toml", exact["name"] == "billing-api", exact["name"])
    report.check("python framework is FastAPI", exact.get("framework") == "FastAPI", str(exact.get("framework")))
    report.check(
        "CODEX_THREAD_ID links the exact Codex chat",
        exact.get("session_estimated") is False
        and exact.get("agent") == f"Codex: {CODEX_TITLE_A}"
        and exact["subtitle"].startswith(CODEX_TITLE_A)
        and "~" not in exact["subtitle"].split("·")[0],
        json.dumps({k: exact.get(k) for k in ("agent", "subtitle", "session_estimated")}),
    )

    report.check(
        "second Codex chat in the same folder is an estimated match",
        guessed.get("session_estimated") is True
        and guessed.get("agent") == f"Codex: ~{CODEX_TITLE_B}"
        and guessed["subtitle"].startswith(f"~{CODEX_TITLE_B}"),
        json.dumps({k: guessed.get(k) for k in ("agent", "subtitle", "session_estimated", "name")}),
    )
    report.check(
        "list output marks only the estimated session with ~",
        f"~{CODEX_TITLE_B}" in text.stdout
        and CLAUDE_TITLE in text.stdout
        and CODEX_TITLE_A in text.stdout
        and f"~{CODEX_TITLE_A}" not in text.stdout
        and f"~{CLAUDE_TITLE}" not in text.stdout,
        text.stdout,
    )

    report.check("vite server is studio-vite", vite["name"] == "studio-vite" and vite.get("framework") == "Vite 6", json.dumps(vite))
    report.check(
        "protected database has no restart",
        protected.get("protected") is True
        and protected.get("can_restart") is False
        and protected.get("process_name", protected.get("name")) is not None,
        json.dumps({k: protected.get(k) for k in ("name", "protected", "can_restart", "command")}),
    )
    report.check(
        "protected command is the postgres comm",
        "postgres" in json.dumps(protected),
        json.dumps(protected),
    )

    report.check(
        "deleted-worktree server is listed before its folder is removed",
        deleted["name"] == "billing-api-invoices" and "(deleted)" not in json.dumps(deleted),
        json.dumps(deleted),
    )
    report.check(
        "memory grower starts below the leak line",
        grow["name"] == "leak-lab" and grow.get("memory_bytes", 0) < 200 * 1024 * 1024 and grow.get("leaking") is False,
        json.dumps({k: grow.get(k) for k in ("memory", "memory_bytes", "leaking", "attention")}),
    )

    # Remove the worktree while the process is still listening.
    shutil.rmtree(deleted_dir)
    try:
        cleaned = wait_until(
            lambda: _cleanup_is(session, PORT_DELETED, "Worktree deleted"),
            timeout=15,
            desc="Worktree deleted cleanup",
        )
    except Exception as exc:
        current = session.server(PORT_DELETED)
        report.check("deleted worktree is suggested for cleanup", False, f"{exc}\n{json.dumps(current)}")
        cleaned = current or {}
    else:
        report.check("deleted worktree is suggested for cleanup", True)

    blob = json.dumps(cleaned)
    report.check(
        "deleted worktree path has no ' (deleted)' suffix",
        "(deleted)" not in blob
        and cleaned.get("directory") == str(deleted_dir)
        and cleaned.get("name") == "billing-api-invoices"
        and " (deleted)" not in cleaned.get("subtitle", ""),
        blob,
    )
    report.check(
        "deleted worktree cannot be restarted",
        cleaned.get("can_restart") is False and cleaned.get("protected") is False,
        blob,
    )
    listed = session.run()
    report.check(
        "list shows clean up: Worktree deleted and no (deleted) suffix",
        "clean up: Worktree deleted" in listed.stdout and "(deleted)" not in listed.stdout,
        listed.stdout,
    )
    try:
        notes = wait_until(
            lambda: [n for n in session.notifications() if "clean up" in n.get("summary", "").lower() or "Worktree deleted" in n.get("body", "")],
            timeout=10,
            desc="cleanup notification",
        )
    except Exception as exc:
        report.check(
            "cleanup notification names the deleted worktree",
            False,
            f"{exc}\nnotifications={session.notifications()}\ndaemon={session.daemon_log_text()[-1500:]}",
        )
    else:
        body = " ".join(n.get("body", "") for n in notes)
        report.check(
            "cleanup notification names the deleted worktree",
            f":{PORT_DELETED}" in body and "Worktree deleted" in body,
            body,
        )

    # Grow RSS only after the daemon has sampled the small baseline.
    urllib.request.urlopen(f"http://127.0.0.1:{PORT_GROW}/grow", timeout=30).read()
    try:
        leaking = wait_until(
            lambda: _leaking(session),
            timeout=25,
            desc="memory-growth alert",
        )
    except Exception as exc:
        current = session.server(PORT_GROW)
        report.check("memory growth is flagged on the server", False, f"{exc}\n{json.dumps(current)}")
        leaking = current or {}
    else:
        report.check(
            "memory growth is flagged on the server",
            leaking.get("leaking") is True and leaking.get("attention") is True and "in 10 min" in (leaking.get("growth") or ""),
            json.dumps({k: leaking.get(k) for k in ("memory", "memory_bytes", "growth", "leaking", "attention", "cleanup", "color")}),
        )
    report.check(
        "leak cleanup label is present and the row turns amber",
        "in 10m" in (leaking.get("cleanup") or "") and leaking.get("color") == "#FFB224",
        json.dumps({k: leaking.get(k) for k in ("cleanup", "color")}),
    )
    try:
        alerts = wait_until(
            lambda: [n for n in session.notifications() if "needs attention" in n.get("summary", "")],
            timeout=10,
            desc="attention notification",
        )
    except Exception as exc:
        report.check(
            "memory-growth notification says the server grew",
            False,
            f"{exc}\nnotifications={session.notifications()}\ndaemon={session.daemon_log_text()[-1500:]}",
        )
    else:
        grew = [n for n in alerts if "grew by" in n.get("body", "")]
        report.check(
            "memory-growth notification says the server grew",
            len(grew) == 1
            and "leak-lab" in grew[0]["body"]
            and "in 10 minutes" in grew[0]["body"]
            and grew[0]["summary"] == f"Port {PORT_GROW} needs attention"
            and grew[0]["actions"][:4] == ["stop", "Stop", "snooze", "Mute alerts"],
            json.dumps(alerts),
        )

    missing = session.run("stop", str(PORT_MISSING))
    report.check(
        "stop of an unknown port returns the real error",
        missing.returncode == 1 and missing.stderr.strip() == f"wtp: no dev server on :{PORT_MISSING}",
        f"rc={missing.returncode} stderr={missing.stderr!r} stdout={missing.stdout!r}",
    )
    protected_stop = session.run("stop", str(PORT_POSTGRES))
    report.check(
        "stop of a protected database returns the real error",
        protected_stop.returncode == 1
        and protected_stop.stderr.strip() == f"wtp: :{PORT_POSTGRES} is protected (postgres)",
        f"rc={protected_stop.returncode} stderr={protected_stop.stderr!r}",
    )
    protected_restart = session.run("restart", str(PORT_POSTGRES))
    report.check(
        "restart of a protected database returns the real error",
        protected_restart.returncode == 1
        and protected_restart.stderr.strip() == f"wtp: :{PORT_POSTGRES} is protected (postgres)",
        f"rc={protected_restart.returncode} stderr={protected_restart.stderr!r}",
    )
    report.check("protected database is still alive and still listed", pid_alive(postgres_pid) and session.server(PORT_POSTGRES) is not None)

    deleted_restart = session.run("restart", str(PORT_DELETED))
    report.check(
        "restart of a deleted worktree returns the real error",
        deleted_restart.returncode == 1
        and deleted_restart.stderr.strip() == f"wtp: don't know how to restart :{PORT_DELETED}",
        f"rc={deleted_restart.returncode} stderr={deleted_restart.stderr!r}",
    )
    report.check("failed restart left the deleted-worktree process running", pid_alive(deleted_pid))

    stopped = session.run("stop", str(PORT_STOP))
    report.check("stop of a real server is accepted", stopped.returncode == 0, stopped.stderr)
    try:
        wait_until(
            lambda: session.server(PORT_STOP) is None and port_closed(PORT_STOP) and not pid_alive(stop_pid),
            timeout=12,
            desc="studio-vite to exit",
        )
        report.check("stopped server leaves the list and dies", True)
    except Exception as exc:
        report.check("stopped server leaves the list and dies", False, str(exc))

    restarted = session.run("restart", str(PORT_CLAUDE))
    report.check("restart of a real server is accepted", restarted.returncode == 0, restarted.stderr)
    log_path = session.home / ".local" / "state" / "whattheport" / f"port-{PORT_CLAUDE}.log"
    try:
        def restarted_process():
            if pid_alive(claude_pid) or not log_path.exists():
                return None
            row = session.server(PORT_CLAUDE)
            if not row or not wait_port(PORT_CLAUDE, timeout=0.3):
                return None
            return row

        again = wait_until(restarted_process, timeout=20, desc="restarted web-dashboard")
        # Restart replaces the process. Track the new pid so teardown does not
        # leave it listening after this home directory is removed.
        new_pid = int(str(again["key"]).split("-")[1])
        session.server_pids = [new_pid if pid == claude_pid else pid for pid in session.server_pids]
        report.check("restarted server is a new process", not pid_alive(claude_pid) and pid_alive(new_pid))
        report.check("restart writes a port log", log_path.exists(), str(log_path))
    except Exception as exc:
        again = session.server(PORT_CLAUDE) or {}
        report.check(
            "restarted server is a new process",
            False,
            f"{exc} old_pid_alive={pid_alive(claude_pid)} log={log_path.exists()} server={again}\ndaemon={session.daemon_log_text()[-1500:]}",
        )
        report.check("restart writes a port log", log_path.exists(), str(log_path))
    report.check(
        "restarted server keeps its Claude session",
        again.get("agent") == f"Claude Code: {CLAUDE_TITLE}" and again.get("session_estimated") is False,
        json.dumps({k: again.get(k) for k in ("agent", "subtitle", "session_estimated", "command", "directory")}),
    )

    opened = session.run("open", str(PORT_CODEX_EXACT))
    report.check("open is accepted", opened.returncode == 0, opened.stderr)
    try:
        wait_until(
            lambda: session.open_log.exists() and f"http://localhost:{PORT_CODEX_EXACT}" in session.open_log.read_text(encoding="utf-8"),
            timeout=5,
            desc="xdg-open log",
        )
        report.check("open asks xdg-open for the server URL", True)
    except Exception as exc:
        report.check("open asks xdg-open for the server URL", False, str(exc))

    second = session.run("daemon")
    report.check(
        "a second daemon reports that it is already running",
        second.returncode == 0 and second.stderr.strip() == "wtp: already running",
        f"rc={second.returncode} stderr={second.stderr!r}",
    )

    quit_proc = session.run("quit")
    report.check("quit stops the daemon", quit_proc.returncode == 0, quit_proc.stderr)
    try:
        wait_until(lambda: session.daemon and session.daemon.poll() is not None, timeout=8, desc="daemon exit")
        report.check("daemon process exits after quit", True)
    except Exception as exc:
        report.check("daemon process exits after quit", False, f"{exc} log={session.daemon_log_text()[-800:]}")
    again_quit = session.run("quit")
    report.check(
        "quit when nothing is running returns the real error",
        again_quit.returncode == 1 and again_quit.stderr.strip() == "wtp: not running",
        f"rc={again_quit.returncode} stderr={again_quit.stderr!r}",
    )

    # Without a daemon the CLI scans /proc itself.
    local = session.run("--json", timeout=20)
    report.check("wtp --json works with no daemon", local.returncode == 0, local.stderr)
    if local.returncode == 0:
        local_servers = json.loads(local.stdout)
        ports = {row["port"] for row in local_servers}
        report.check(
            "local scan still sees the surviving servers and not the stopped one",
            {PORT_CODEX_EXACT, PORT_CODEX_ESTIMATED, PORT_POSTGRES, PORT_GROW, PORT_DELETED} <= ports
            and PORT_STOP not in ports,
            str(sorted(ports)),
        )
        local_exact = by_port(local_servers, PORT_CODEX_EXACT)
        local_guessed = by_port(local_servers, PORT_CODEX_ESTIMATED)
        report.check(
            "local scan keeps the exact and estimated Codex links",
            local_exact.get("session_estimated") is False
            and local_exact.get("agent") == f"Codex: {CODEX_TITLE_A}"
            and local_guessed.get("session_estimated") is True
            and str(local_guessed.get("agent", "")).startswith("Codex: ~"),
            json.dumps(
                [
                    {k: local_exact.get(k) for k in ("agent", "session_estimated")},
                    {k: local_guessed.get(k) for k in ("agent", "session_estimated")},
                ]
            ),
        )

    # daemon --tray must stay up even when no StatusNotifier host is running.
    session.start_daemon(tray=True)
    try:
        wait_until(lambda: session.server(PORT_POSTGRES) is not None, timeout=15, desc="tray-mode daemon scan")
        tray_ok = True
        tray_detail = ""
    except Exception as exc:
        tray_ok = False
        tray_detail = f"{exc}\n{session.daemon_log_text()[-1500:]}"
    report.check("wtp daemon --tray serves the CLI without a tray host", tray_ok, tray_detail)
    if "tray unavailable" in session.daemon_log_text():
        report.note("daemon --tray logged 'tray unavailable' because no StatusNotifier watcher was on the bus; the service still ran")
    session.run("quit")
    time.sleep(0.5)


def _ready(session: Session):
    try:
        servers = session.json_servers()
    except Exception:
        return None
    ports = {row.get("port") for row in servers}
    wanted = {
        PORT_CLAUDE,
        PORT_CODEX_EXACT,
        PORT_CODEX_ESTIMATED,
        PORT_STOP,
        PORT_DELETED,
        PORT_GROW,
        PORT_POSTGRES,
    }
    if not wanted <= ports:
        return None
    return servers


def _cleanup_is(session: Session, port: int, reason: str):
    row = session.server(port)
    if row and row.get("cleanup") == reason:
        return row
    return None


def _leaking(session: Session):
    row = session.server(PORT_GROW)
    if row and row.get("leaking") is True:
        return row
    return None


if __name__ == "__main__":
    raise SystemExit(main())
