#!/usr/bin/env python3
"""UI end-to-end tests.

Tray: Xvfb + Openbox + xfce4-panel's StatusNotifier plugin. The menu is opened
with xdotool and read back through AT-SPI (dogtail).

GNOME: Xvfb + ``gnome-shell --x11`` with the WhatThePort extension enabled. The
panel button is driven with xdotool from its accessible coordinates, then the
open menu is checked against the daemon's GetState data. The memory bar is
checked by looking for each server's colour in the screenshot.

``gnome-shell --headless`` is not used. Without Xwayland the headless shell
starts mutter, but AT-SPI exposes no children and ``org.gnome.Shell.Screenshot``
is not on the bus, so the menu cannot be driven or captured. ``--x11`` under
Xvfb needs a system bus (the shell exits in ``endSessionDialog.js`` without one).

Run ``python3 e2e/test_ui.py`` for both, or pass ``tray`` / ``gnome``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

os.environ.setdefault("GTK_MODULES", "gail:atk-bridge")
os.environ.setdefault("NO_AT_BRIDGE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    CheckFailure,
    CLAUDE_ID,
    CLAUDE_TITLE,
    CODEX_TITLE_A,
    CODEX_TITLE_B,
    PORT_CLAUDE,
    PORT_CODEX_ESTIMATED,
    PORT_CODEX_EXACT,
    PORT_DELETED,
    PORT_POSTGRES,
    THREAD_A,
    Report,
    Session,
    WTP,
    wait_until,
)

ROOT = Path(__file__).resolve().parents[1]
EXT_SRC = ROOT / "gnome-extension" / "whattheport@local"
ART = ROOT / "e2e" / "artifacts" / "ui"
PUB = Path("/opt/cursor/artifacts/screenshots")

PANEL_XML = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <channel name="xfce4-panel" version="1.0">
      <property name="configver" type="int" value="2"/>
      <property name="panels" type="array">
        <value type="int" value="1"/>
        <property name="panel-1" type="empty">
          <property name="position" type="string" value="p=6;x=0;y=0"/>
          <property name="length" type="uint" value="100"/>
          <property name="position-locked" type="bool" value="true"/>
          <property name="size" type="uint" value="36"/>
          <property name="plugin-ids" type="array">
            <value type="int" value="1"/>
          </property>
        </property>
      </property>
      <property name="plugins" type="empty">
        <property name="plugin-1" type="string" value="sntray"/>
      </property>
    </channel>
    """
)


def _log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "ab", buffering=0)


class Display:
    def __init__(self, number: int, env: dict):
        self.number = number
        self.env = env
        self.proc = subprocess.Popen(
            ["Xvfb", f":{number}", "-screen", "0", "1400x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.env["DISPLAY"] = f":{number}"
        deadline = time.time() + 5
        while time.time() < deadline:
            if subprocess.run(["xdpyinfo"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError(f"Xvfb :{number} did not come up")

    def close(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)


def screenshot(env: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["scrot", "-o", str(path)], env=env, check=True, capture_output=True)


def icon_center(path: Path) -> tuple[int, int] | None:
    from PIL import Image

    image = Image.open(path)
    xs: list[int] = []
    ys: list[int] = []
    width, height = image.size
    for y in range(min(height, 80)):
        for x in range(width):
            r, g, b = image.getpixel((x, y))[:3]
            if abs(r - 110) < 25 and abs(g - 199) < 25 and abs(b - 237) < 25 and b > r + 40:
                xs.append(x)
                ys.append(y)
    if len(xs) < 20:
        return None
    return int(sum(xs) / len(xs)), int(sum(ys) / len(ys))


def click(env: dict, x: int, y: int) -> None:
    subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], env=env, check=True)


def _teardown(session: Session, extras: list[subprocess.Popen], display: Display | None, tmp: Path) -> None:
    # AT-SPI delivers SIGTERM to this process when the session bus it is
    # attached to goes away. The checks have already been recorded.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        session.close()
    except Exception:
        pass
    for proc in extras:
        if proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    if display:
        try:
            os.kill(display.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def publish(path: Path) -> None:
    PUB.mkdir(parents=True, exist_ok=True)
    shutil.copy(path, PUB / path.name)


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def prepare_projects(session: Session) -> dict[str, Path]:
    claude = session.projects / "web-dashboard"
    codex = session.projects / "codex-app"
    deleted = session.projects / "billing-api-invoices"
    db = session.projects / "local-db"
    claude.mkdir(parents=True)
    (claude / "package.json").write_text(
        '{"name":"web-dashboard","dependencies":{"next":"15.2.0"}}',
        encoding="utf-8",
    )
    session.git_init(claude)
    codex.mkdir()
    (codex / "pyproject.toml").write_text('[project]\nname = "billing-api"\ndependencies = ["fastapi"]\n', encoding="utf-8")
    session.git_init(codex)
    session.write_agent_fixtures(codex)
    deleted.mkdir()
    (deleted / "package.json").write_text('{"name":"billing-api-invoices"}', encoding="utf-8")
    db.mkdir()
    (db / "package.json").write_text('{"name":"local-db"}', encoding="utf-8")
    return {"claude": claude, "codex": codex, "deleted": deleted, "db": db}


def start_servers(session: Session, projects: dict[str, Path]) -> None:
    session.spawn_node(projects["claude"], PORT_CLAUDE, {"CLAUDE_CODE_SESSION_ID": CLAUDE_ID})
    session.spawn_python(projects["codex"], PORT_CODEX_EXACT, {"CODEX_THREAD_ID": THREAD_A})
    session.spawn_python(projects["codex"], PORT_CODEX_ESTIMATED, {})
    session.spawn_python(projects["deleted"], PORT_DELETED, {})
    session.spawn_postgres(projects["db"], PORT_POSTGRES)
    shutil.rmtree(projects["deleted"])


def adopt_env(session: Session) -> None:
    """Copy the isolated session into this process before libdbus starts.

    libdbus caches the session-bus address the first time it is used. The
    runner inherits ``autolaunch:`` from the desktop session; opening the
    system bus (or importing AT-SPI) before this copy makes every later
    ``SessionBus()`` miss the daemon.
    """
    for key in (
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_CURRENT_DESKTOP",
        "GTK_MODULES",
        "PATH",
    ):
        value = session.env.get(key)
        if value:
            os.environ[key] = value


def session_bus(session: Session):
    import dbus

    # A private connection ignores the cached shared SessionBus.
    return dbus.bus.BusConnection(session.env["DBUS_SESSION_BUS_ADDRESS"])


def state_of(session: Session) -> dict:
    import dbus
    import json

    bus = session_bus(session)
    proxy = bus.get_object("dev.whattheport.WhatThePort", "/dev/whattheport/Daemon")
    raw = proxy.GetState(dbus_interface="dev.whattheport.Daemon1")
    return json.loads(str(raw))


def wait_for_servers(session: Session, timeout: float, desc: str) -> dict:
    want = {PORT_CLAUDE, PORT_CODEX_EXACT, PORT_CODEX_ESTIMATED, PORT_DELETED, PORT_POSTGRES}
    deadline = time.time() + timeout
    last = "not called"
    while time.time() < deadline:
        try:
            state = state_of(session)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.4)
            continue
        ports = {s.get("port") for s in state.get("servers", [])}
        if want <= ports:
            return state
        last = f"ports={sorted(p for p in ports if isinstance(p, int))}"
        time.sleep(0.4)
    raise CheckFailure(f"timed out after {timeout:.0f}s waiting for {desc}; last={last}")


def ensure_system_bus() -> None:
    import dbus

    try:
        dbus.SystemBus()
        return
    except Exception:
        pass
    subprocess.run(["sudo", "-n", "rm", "-f", "/run/dbus/pid"], check=False)
    proc = subprocess.run(["sudo", "-n", "dbus-daemon", "--system", "--fork"], capture_output=True, text=True)
    time.sleep(0.3)
    try:
        dbus.SystemBus()
    except Exception as exc:
        raise RuntimeError(f"system bus is not available ({exc}); dbus-daemon said {proc.stderr.strip()}") from exc


def tray_menu_nodes(env: dict):
    from dogtail import tree

    os.environ["DBUS_SESSION_BUS_ADDRESS"] = env["DBUS_SESSION_BUS_ADDRESS"]
    os.environ["DISPLAY"] = env["DISPLAY"]
    root = tree.root
    for _ in range(20):
        for app in root.applications():
            if app.name == "wrapper-2.0":
                menus = [n for n in walk(app) if n.roleName == "menu" and n.name.startswith(":")]
                if menus:
                    return app
        time.sleep(0.25)
    return None


def test_tray() -> int:
    report = Report()
    ART.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="wtp-ui-tray-"))
    os.environ["WTP_KEEP_DISPLAY"] = ":99"
    session = Session(tmp)
    display = None
    extras: list[subprocess.Popen] = []
    try:
        if not WTP.exists():
            raise RuntimeError(f"missing {WTP}")
        display = Display(99, session.env)
        session.env["GTK_MODULES"] = "gail:atk-bridge"
        session.env["XDG_CURRENT_DESKTOP"] = "XFCE"
        session.start_bus()
        adopt_env(session)
        projects = prepare_projects(session)
        start_servers(session, projects)

        xml_dir = session.home / ".config" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
        xml_dir.mkdir(parents=True)
        (xml_dir / "xfce4-panel.xml").write_text(PANEL_XML, encoding="utf-8")
        extras.append(subprocess.Popen(["openbox"], env=session.env, stdout=_log(session.logs / "openbox.log"), stderr=subprocess.STDOUT, start_new_session=True))
        time.sleep(0.3)
        extras.append(
            subprocess.Popen(
                ["xfce4-panel", "--disable-wm-check"],
                env=session.env,
                stdout=_log(session.logs / "panel.log"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
        bus = session_bus(session)

        def watcher():
            names = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus").ListNames(
                dbus_interface="org.freedesktop.DBus"
            )
            return "org.kde.StatusNotifierWatcher" in [str(n) for n in names]

        wait_until(watcher, 15, desc="StatusNotifier watcher")
        session.start_daemon(tray=True)
        state = wait_for_servers(session, 20, "daemon state")
        shot = ART / "tray-icon.png"
        center = None
        deadline = time.time() + 10
        while time.time() < deadline:
            screenshot(session.env, shot)
            center = icon_center(shot)
            if center:
                break
            time.sleep(0.4)
        report.check("tray icon is drawn on the panel", center is not None, session.daemon_log_text()[-800:])
        if center:
            click(session.env, *center)
            time.sleep(0.6)
            menu_shot = ART / "tray-menu.png"
            screenshot(session.env, menu_shot)
            publish(shot)
            publish(menu_shot)

            app = tray_menu_nodes(session.env)
            if app is None:
                report.check("tray menu is exposed to AT-SPI", False, "wrapper-2.0 had no server menus")
            else:
                app.dump(fileName=str(ART / "tray-a11y.txt"))
                report.check("tray menu is exposed to AT-SPI", True)
                # Free memory moves while the panel starts, so compare against
                # the daemon at the moment the menu is open.
                _assert_menu(report, app, state_of(session), kind="tray")
        else:
            publish(shot)
    except Exception as exc:
        report.check("tray scenario ran", False, f"{type(exc).__name__}: {exc}")
    finally:
        code = report.finish(ART / "tray-report.json")
        _teardown(session, extras, display, tmp)
    return code


def _assert_menu(report: Report, app, state: dict, kind: str) -> None:
    nodes = list(walk(app))
    names = [n.name for n in nodes if n.name]
    blob = "\n".join(names)
    (ART / f"{kind}-menu-names.txt").write_text(blob + "\n", encoding="utf-8")
    summary = state["summary"]
    summary_line = f"Servers {summary['servers_memory']} · Other {summary['other_memory']} · Free {summary['free_memory']}"
    if kind == "tray":
        report.check("tray summary matches the daemon", summary_line in names, f"wanted {summary_line!r}\n{blob[:500]}")
    else:
        report.check(
            "GNOME menu shows the daemon memory summary",
            summary["servers_memory"] in blob and summary["free_memory"] in blob,
            blob[:800],
        )
    report.check(f"{kind} menu has no ' (deleted)' suffix", "(deleted)" not in blob, blob)

    by_port = {s["port"]: s for s in state["servers"]}
    for port, title, estimated in (
        (PORT_CLAUDE, CLAUDE_TITLE, False),
        (PORT_CODEX_EXACT, CODEX_TITLE_A, False),
        (PORT_CODEX_ESTIMATED, f"~{CODEX_TITLE_B}", True),
    ):
        server = by_port[port]
        report.check(
            f"{kind} shows {server['name']} on :{port}",
            str(port) in blob and server["name"] in blob and title in blob,
            f"wanted {title!r} in menu",
        )
        report.check(
            f"{kind} estimated flag for :{port} matches the daemon",
            server.get("session_estimated") is estimated,
            str(server.get("session_estimated")),
        )

    if kind == "tray":
        deleted = _submenu(nodes, by_port[PORT_DELETED])
        protected = _submenu(nodes, by_port[PORT_POSTGRES])
        report.check("deleted-worktree tray submenu was found", deleted is not None)
        report.check("protected tray submenu was found", protected is not None)
        if deleted is not None:
            deleted_names = [n.name for n in walk(deleted)]
            report.check(
                "deleted worktree has Stop and no Restart",
                "Stop" in deleted_names and "Restart" not in deleted_names and any("Worktree deleted" in n for n in deleted_names),
                " | ".join(deleted_names),
            )
        if protected is not None:
            protected_names = [n.name for n in walk(protected)]
            report.check(
                "protected database offers Open and neither Stop nor Restart",
                any(n.startswith("Open http://") for n in protected_names)
                and "Stop" not in protected_names
                and "Restart" not in protected_names,
                " | ".join(protected_names),
            )
        if state.get("cleanup"):
            report.check(
                "tray cleanup item matches the daemon",
                state["cleanup"]["label"] in names,
                state["cleanup"]["label"],
            )


def _submenu(nodes, server: dict):
    prefix = f":{server['port']}  {server['name']}  —"
    for node in nodes:
        if node.roleName == "menu" and node.name.startswith(prefix):
            return node
    return None


def _gnome_app():
    from dogtail import tree

    for app in tree.root.applications():
        if app.name == "gnome-shell":
            return app
    return None


def _showing_names(app) -> list[str]:
    names = []
    for node in walk(app):
        if not node.name:
            continue
        try:
            visible = bool(node.showing)
        except Exception:
            visible = True
        if visible:
            names.append(node.name)
    return names


def _click_name(env: dict, app, text: str) -> bool:
    for node in walk(app):
        if node.name != text:
            continue
        try:
            if not node.showing:
                continue
            x, y = node.position
            w, h = node.size
        except Exception:
            continue
        if w <= 0 or h <= 0:
            continue
        click(env, int(x + w / 2), int(y + h / 2))
        return True
    return False


def test_gnome() -> int:
    report = Report()
    ART.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="wtp-ui-gnome-"))
    os.environ["WTP_KEEP_DISPLAY"] = ":98"
    session = Session(tmp)
    display = None
    shell = None
    extras: list[subprocess.Popen] = []
    code = 1
    try:
        # Xvfb and the private bus must exist before anything imports libdbus.
        display = Display(98, session.env)
        session.env["GTK_MODULES"] = "gail:atk-bridge"
        session.env["XDG_CURRENT_DESKTOP"] = "GNOME"
        session.env["XDG_SESSION_TYPE"] = "x11"
        session.env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        session.start_bus()
        adopt_env(session)
        ensure_system_bus()
        ext_dst = session.home / ".local/share/gnome-shell/extensions/whattheport@local"
        shutil.copytree(EXT_SRC, ext_dst)
        for args in (
            ["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"],
            ["gsettings", "set", "org.gnome.shell", "disable-user-extensions", "false"],
            ["gsettings", "set", "org.gnome.shell", "enabled-extensions", "['whattheport@local']"],
        ):
            proc = subprocess.run(args, env=session.env, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr}")
        shell_log = session.logs / "gnome-shell.log"
        shell = subprocess.Popen(
            ["gnome-shell", "--x11", "--replace"],
            env=session.env,
            stdout=_log(shell_log),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 25
        started = False
        while time.time() < deadline:
            if shell.poll() is not None:
                break
            if "GNOME Shell started" in shell_log.read_text(errors="replace"):
                started = True
                break
            time.sleep(0.3)
        log_tail = shell_log.read_text(errors="replace")[-2000:]
        report.check("GNOME Shell started under Xvfb", started and shell.poll() is None, log_tail)
        if started:
            projects = prepare_projects(session)
            start_servers(session, projects)
            session.start_daemon(tray=False)
            try:
                state = wait_for_servers(session, 20, "daemon state for GNOME")
            except CheckFailure as exc:
                detail = f"{exc}\n{session.daemon_log_text()[-2000:]}"
                (ART / "gnome-daemon.log").write_text(session.daemon_log_text(), encoding="utf-8")
                report.check("daemon published the fixture servers to GNOME", False, detail)
            else:
                report.check("daemon published the fixture servers to GNOME", True)
                _drive_gnome_menu(report, session, state, log_tail)
    except Exception as exc:
        report.check("GNOME scenario ran", False, f"{type(exc).__name__}: {exc}")
        if session.logs.joinpath("gnome-shell.log").exists():
            report.note(session.logs.joinpath("gnome-shell.log").read_text(errors="replace")[-1500:])
    finally:
        if shell and shell.poll() is None:
            extras.append(shell)
        code = report.finish(ART / "gnome-report.json")
        _teardown(session, extras, display, tmp)
    return code


def _drive_gnome_menu(report: Report, session: Session, state: dict, log_tail: str) -> None:
    button = None
    deadline = time.time() + 20
    while time.time() < deadline and button is None:
        app = _gnome_app()
        if app is not None:
            for node in walk(app):
                if node.name == "WhatThePort":
                    button = node
                    break
        if button is None:
            time.sleep(0.4)
    report.check("WhatThePort panel button is in the accessibility tree", button is not None, log_tail)
    if button is None:
        shot = ART / "gnome-no-icon.png"
        screenshot(session.env, shot)
        publish(shot)
        return

    x, y = button.position
    w, h = button.size
    click(session.env, x + max(w, 1) // 2, y + max(h, 1) // 2)
    time.sleep(0.8)
    opened = ART / "gnome-menu.png"
    screenshot(session.env, opened)
    publish(opened)

    shell_app = _gnome_app()
    if shell_app is None:
        report.check("GNOME Shell stays in the accessibility tree", False)
        return
    shell_app.dump(fileName=str(ART / "gnome-a11y.txt"))
    names = _showing_names(shell_app)
    (ART / "gnome-menu-names.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    # Re-read after the shell has started; machine-wide free memory moves.
    state = state_of(session)
    _assert_menu(report, shell_app, state, kind="gnome")
    _assert_gnome_rows(report, state, names)
    _assert_bar_colors(report, opened, state)

    expanded = ART / "gnome-row.png"
    if _click_name(session.env, shell_app, "web-dashboard"):
        time.sleep(0.5)
        screenshot(session.env, expanded)
        publish(expanded)
        shell_app = _gnome_app() or shell_app
        open_names = _showing_names(shell_app)
        report.check(
            "expanding a normal server reveals Restart",
            "Restart" in open_names and "Stop" in open_names,
            " | ".join(open_names),
        )
        _click_name(session.env, shell_app, "web-dashboard")
        time.sleep(0.4)
    else:
        report.check("expanding a normal server reveals Restart", False, "could not click web-dashboard")

    shell_app = _gnome_app() or shell_app
    protected = ART / "gnome-protected.png"
    if _click_name(session.env, shell_app, "local-db"):
        time.sleep(0.5)
        screenshot(session.env, protected)
        publish(protected)
        shell_app = _gnome_app() or shell_app
        protected_names = _showing_names(shell_app)
        report.check(
            "protected database row has no Restart",
            "Restart" not in protected_names,
            " | ".join(protected_names),
        )
        report.check(
            "protected database row has no Stop action",
            "Stop" not in protected_names,
            " | ".join(n for n in protected_names if "Stop" in n or "Open" in n),
        )
    else:
        report.check("protected database row has no Restart", False, "could not click local-db")

    closed = ART / "gnome-panel.png"
    subprocess.run(["xdotool", "key", "Escape"], env=session.env, check=False)
    time.sleep(0.3)
    screenshot(session.env, closed)
    publish(closed)


def _assert_gnome_rows(report: Report, state: dict, names: list[str]) -> None:
    blob = "\n".join(names)
    report.check(
        "GNOME menu names the deleted worktree",
        "billing-api-invoices" in blob and "Worktree deleted" in blob,
        blob[:1000],
    )
    report.check(
        "GNOME menu names the protected database",
        "local-db" in blob and ":18060" in blob,
        blob[:1000],
    )
    if state.get("cleanup"):
        report.check(
            "GNOME cleanup item matches the daemon",
            state["cleanup"]["label"] in names,
            state["cleanup"]["label"],
        )
    # The header Stop icon is an accessible "Stop server" button, hidden when protected.
    # Restart lives in the collapsed submenu and is checked after that row is opened.
    unprotected = [s for s in state["servers"] if not s.get("protected")]
    stop_icons = [n for n in names if n == "Stop server"]
    report.check(
        "GNOME hides Stop on the protected database",
        len(stop_icons) == len(unprotected),
        f"stop icons={len(stop_icons)} unprotected={len(unprotected)} names={stop_icons}",
    )


def _assert_bar_colors(report: Report, shot: Path, state: dict) -> None:
    from PIL import Image

    image = Image.open(shot).convert("RGB")
    pixels = list(image.getdata())
    missing = []
    for segment in state["summary"]["segments"]:
        color = segment["color"].lstrip("#")
        want = tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))
        hits = 0
        for r, g, b in pixels:
            if abs(r - want[0]) <= 18 and abs(g - want[1]) <= 18 and abs(b - want[2]) <= 18:
                hits += 1
                if hits >= 12:
                    break
        if hits < 12:
            missing.append(f":{segment['port']} {segment['color']} ({hits} px)")
    report.check(
        "GNOME memory bar uses each server colour from the daemon",
        not missing,
        ", ".join(missing) if missing else f"{len(state['summary']['segments'])} colours",
    )


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which == "all":
        tray = subprocess.call([sys.executable, __file__, "tray"])
        gnome = subprocess.call([sys.executable, __file__, "gnome"])
        return 1 if tray or gnome else 0
    if which == "tray":
        return test_tray()
    if which == "gnome":
        return test_gnome()
    print(f"unknown mode {which}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
