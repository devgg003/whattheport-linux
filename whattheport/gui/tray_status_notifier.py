"""FreeDesktop StatusNotifierItem (SNI) + Ayatana Label + DBusMenu implementation for WhatThePort."""
import json
import os
import subprocess
import time
from typing import Callable, Optional
import gi
gi.require_version('Gio', '2.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gio, GLib

from whattheport.engine.control import ProcessControl
from whattheport.engine.format import bytes_str, duration, percent, port_color, total_str
from whattheport.engine.monitor import ServerMonitor
from whattheport.engine.prefs import Preferences
from whattheport.engine.server import Server, ServerStatus

BUS_NAME = "dev.whattheport.WhatThePort"
GLANCE_PATH = "/dev/whattheport/Glance"

# Note: Omitting <method name="Activate"> intentionally tells Ubuntu's
# ubuntu-appindicators@ubuntu.com extension that single-left-click should
# immediately open the dropdown menu without waiting for double-click.
SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="ToolTip" type="(sa(ii)ss)" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <method name="ContextMenu">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="orientation" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewMenu"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg type="s" name="status"/>
    </signal>
    <signal name="XAyatanaNewLabel">
      <arg type="s" name="label"/>
      <arg type="s" name="guide"/>
    </signal>
  </interface>
</node>
"""

DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
  </interface>
</node>
"""

GLANCE_XML = """
<node>
  <interface name="dev.whattheport.Glance">
    <method name="GetGlance">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="OpenServer">
      <arg type="u" name="port" direction="in"/>
    </method>
    <method name="StopServer">
      <arg type="u" name="port" direction="in"/>
    </method>
    <method name="Quit"/>
    <method name="OpenDashboard"/>
  </interface>
</node>
"""

class StatusNotifierTray:
    def __init__(
        self,
        monitor: ServerMonitor,
        on_open_dashboard: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None
    ):
        self.monitor = monitor
        self.on_open_dashboard = on_open_dashboard
        self.on_quit = on_quit
        self.connection: Optional[Gio.DBusConnection] = None
        self.sni_reg_id = 0
        self.menu_reg_id = 0
        self.glance_reg_id = 0
        self.revision = 1
        self.action_callbacks = {}
        self._retired = {}  # port -> {"server", "state", "since"}
        self.icons_dir = os.path.join(os.path.dirname(__file__), "icons")

        self.monitor.add_listener(self._on_monitor_updated)

    def start(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.connection = bus

        # Export StatusNotifierItem
        sni_node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self.sni_reg_id = bus.register_object(
            "/StatusNotifierItem",
            sni_node.interfaces[0],
            self._handle_sni_method,
            self._handle_sni_prop_get,
            None
        )

        # Export com.canonical.dbusmenu
        menu_node = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML)
        self.menu_reg_id = bus.register_object(
            "/StatusNotifierMenu",
            menu_node.interfaces[0],
            self._handle_menu_method,
            self._handle_menu_prop_get,
            None
        )

        glance_node = Gio.DBusNodeInfo.new_for_xml(GLANCE_XML)
        self.glance_reg_id = bus.register_object(
            GLANCE_PATH,
            glance_node.interfaces[0],
            self._handle_glance_method,
            None,
            None
        )
        self._own_bus_name()

        # Register with StatusNotifierWatcher
        try:
            bus.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", ("/StatusNotifierItem",)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None
            )
        except Exception as e:
            print(f"StatusNotifierWatcher registration notice: {e}")

    def _own_bus_name(self):
        """Expose GetGlance on the app id so the top-bar hover card can read it."""
        if not self.connection:
            return
        try:
            # 4 = DO_NOT_QUEUE. Same-connection owners get ALREADY_OWNER.
            self.connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "RequestName",
                GLib.Variant("(su)", (BUS_NAME, 4)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None
            )
        except Exception as e:
            print(f"WhatThePort glance bus name: {e}")

    def glance_data(self) -> dict:
        monitor = self.monitor
        threshold = monitor.prefs.threshold_bytes
        active, retired = self._partition_servers()
        servers = []
        for server in active:
            detail = []
            if server.agent and (server.agent.title or server.agent.kind):
                detail.append(server.agent.title or server.agent.kind)
            elif server.project and server.project.branch:
                detail.append(server.project.branch)
            elif not server.cwd_exists:
                detail.append("Worktree deleted")
            detail.append(f"up {duration(server.uptime)}")
            attention = server.is_leaking() or server.status(threshold) == ServerStatus.ATTENTION
            servers.append({
                "port": server.port,
                "name": server.display_name,
                "memory": bytes_str(server.memory),
                "cpu": percent(server.cpu),
                "detail": " · ".join(detail),
                "color": port_color(monitor.color_index(server.port), dark=True),
                "attention": attention,
                "protected": server.is_protected,
            })
        return {
            "memory": total_str(sum(s.memory for s in active)),
            "cpu": percent(monitor.total_server_cpu),
            "count": len(servers),
            "servers": servers,
            "retired": [
                {
                    "port": entry["server"].port,
                    "name": entry["server"].display_name,
                    "state": entry["state"],
                }
                for entry in retired
            ],
        }

    def _live_server(self, port: int) -> Optional[Server]:
        for server in self.monitor.servers:
            if server.port == port:
                return server
        return None

    def _tooltip_text(self) -> tuple:
        data = self.glance_data()
        title = f"{data['memory']}  ·  CPU {data['cpu']}  ·  {self._current_label()}"
        if not data["servers"]:
            return title, "No dev servers running"
        lines = [
            f":{s['port']}   {s['name']}   {s['memory']}"
            for s in data["servers"][:6]
        ]
        extra = data["count"] - len(lines)
        if extra > 0:
            lines.append(f"+{extra} more")
        return title, "\n".join(lines)

    def _handle_glance_method(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetGlance":
            invocation.return_value(GLib.Variant("(s)", (json.dumps(self.glance_data()),)))
            return
        if method == "Quit":
            invocation.return_value(None)
            if self.on_quit:
                GLib.idle_add(self.on_quit)
            return
        if method == "OpenDashboard":
            invocation.return_value(None)
            if self.on_open_dashboard:
                GLib.idle_add(self.on_open_dashboard)
            return
        if method in ("OpenServer", "StopServer"):
            (port,) = params.unpack()
            server = self._live_server(int(port))
            if server is not None:
                if method == "OpenServer":
                    self._open_server(server)
                else:
                    self._stop_server(server)
        invocation.return_value(None)

    def _current_label(self) -> str:
        cnt = len(self.monitor.servers)
        return f"{cnt} server{'s' if cnt != 1 else ''}"

    def _current_icon_path(self) -> str:
        icon_file = "wtp-attention.svg" if self.monitor.needs_attention else "wtp-normal.svg"
        full_path = os.path.join(self.icons_dir, icon_file)
        if os.path.isfile(full_path):
            return full_path
        return "network-server"

    def _on_monitor_updated(self):
        GLib.idle_add(self._emit_updates)

    def _emit_updates(self):
        self.revision += 1
        if self.connection:
            self.connection.emit_signal(
                None,
                "/StatusNotifierMenu",
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self.revision, 0))
            )
            self.connection.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                "XAyatanaNewLabel",
                GLib.Variant("(ss)", (self._current_label(), "99 servers"))
            )
            self.connection.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                "NewIcon",
                None
            )
            self.connection.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                "NewTitle",
                None
            )
            self.connection.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                "NewToolTip",
                None
            )
        return False

    def _handle_sni_prop_get(self, conn, sender, path, iface, prop):
        if prop == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        elif prop == "Id":
            return GLib.Variant("s", "whattheport")
        elif prop == "Title":
            return GLib.Variant("s", f"WhatThePort ({self._current_label()})")
        elif prop == "Status":
            return GLib.Variant("s", "Active")
        elif prop == "WindowId":
            return GLib.Variant("i", 0)
        elif prop == "IconName":
            return GLib.Variant("s", self._current_icon_path())
        elif prop == "IconThemePath":
            return GLib.Variant("s", self.icons_dir if os.path.isdir(self.icons_dir) else "")
        elif prop == "Menu":
            return GLib.Variant("o", "/StatusNotifierMenu")
        elif prop == "ItemIsMenu":
            return GLib.Variant("b", True)
        elif prop == "XAyatanaLabel":
            return GLib.Variant("s", self._current_label())
        elif prop == "XAyatanaLabelGuide":
            return GLib.Variant("s", "99 servers")
        elif prop == "ToolTip":
            title, body = self._tooltip_text()
            return GLib.Variant("(sa(ii)ss)", ("network-server", [], title, body))
        return None

    def _handle_sni_method(self, conn, sender, path, iface, method, params, invocation):
        if method == "SecondaryActivate":
            self._trigger_open_dashboard()
        invocation.return_value(None)

    def _trigger_open_dashboard(self):
        if self.on_open_dashboard:
            GLib.idle_add(self.on_open_dashboard)
        else:
            subprocess.Popen([os.path.expanduser("~/.local/bin/whattheport")])

    def _open_server(self, server: Server):
        subprocess.Popen(["xdg-open", server.url])

    def _stop_server(self, server: Server):
        if server.is_protected or server.port in self._retired:
            return
        seconds = 3.0
        prefs = getattr(self.monitor, "prefs", None)
        if prefs is not None:
            try:
                seconds = float(prefs.get("force_quit_seconds", 3.0))
            except (TypeError, ValueError):
                seconds = 3.0
        self._retired[server.port] = {
            "server": server,
            "state": "stopping",
            "since": time.time(),
        }
        ProcessControl.stop(
            server,
            force_quit_seconds=seconds,
            completion=lambda: GLib.idle_add(self._mark_stopped, server.port),
        )
        self._emit_updates()

    def _mark_stopped(self, port: int):
        entry = self._retired.get(port)
        if entry and entry["state"] != "stopped":
            entry["state"] = "stopped"
            entry["since"] = time.time()
            self._emit_updates()
        return False

    def _server_menu_label(self, server: Server, suffix: str = "") -> str:
        branch = f" ({server.project.branch})" if server.project and server.project.branch else ""
        label = f":{server.port}  {server.display_name}{branch}  ―  {bytes_str(server.memory)}"
        if suffix:
            label = f"{label}  {suffix}"
        return label

    def _partition_servers(self):
        live = {s.port: s for s in self.monitor.servers}
        for port, entry in list(self._retired.items()):
            if port in live and entry["state"] == "stopped":
                del self._retired[port]
                continue
            if port in live:
                entry["server"] = live[port]
            elif entry["state"] == "stopping":
                entry["state"] = "stopped"
                entry["since"] = time.time()
        active = [s for s in self.monitor.servers if s.port not in self._retired]
        retired = list(self._retired.values())
        return active, retired

    def _handle_menu_prop_get(self, conn, sender, path, iface, prop):
        if prop == "Version":
            return GLib.Variant("u", 3)
        elif prop == "TextDirection":
            return GLib.Variant("s", "ltr")
        elif prop == "Status":
            return GLib.Variant("s", "normal")
        elif prop == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def _handle_menu_method(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            parent_id, depth, prop_names = params.unpack()
            layout = self._build_menu_layout()
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, layout)))
        elif method == "Event":
            item_id, event_id, data, timestamp = params.unpack()
            if event_id == "clicked" and item_id in self.action_callbacks:
                self.action_callbacks[item_id]()
            invocation.return_value(None)
        elif method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([],)))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))

    def _build_menu_layout(self):
        self.action_callbacks.clear()

        children = []
        next_id = 1

        active, retired = self._partition_servers()
        tot_mem = sum(s.memory for s in active)

        # Header item: Servers summary
        hdr_id = next_id
        next_id += 1
        summary_text = f"Servers: {bytes_str(tot_mem)} ({len(active)} active)"
        hdr_props = {
            "label": GLib.Variant("s", summary_text),
            "enabled": GLib.Variant("b", False),
        }
        v_hdr = GLib.Variant('(ia{sv}av)', (hdr_id, hdr_props, []))
        children.append(v_hdr)

        # Separator
        sep1_id = next_id
        next_id += 1
        v_sep1 = GLib.Variant('(ia{sv}av)', (sep1_id, {"type": GLib.Variant("s", "separator")}, []))
        children.append(v_sep1)

        # The top-bar menu gives each entry one click. The server name opens
        # the address. The stop icon beside that action is its own entry.
        if not active and not retired:
            empty_id = next_id
            next_id += 1
            v_empty = GLib.Variant(
                '(ia{sv}av)',
                (empty_id, {"label": GLib.Variant("s", "No dev servers listening"), "enabled": GLib.Variant("b", False)}, [])
            )
            children.append(v_empty)
        else:
            for s in active:
                s_id = next_id
                next_id += 1
                s_props = {
                    "label": GLib.Variant("s", self._server_menu_label(s)),
                    "enabled": GLib.Variant("b", True),
                }
                self.action_callbacks[s_id] = lambda srv=s: self._open_server(srv)
                children.append(GLib.Variant('(ia{sv}av)', (s_id, s_props, [])))
                if not s.is_protected:
                    stop_id = next_id
                    next_id += 1
                    self.action_callbacks[stop_id] = lambda srv=s: self._stop_server(srv)
                    children.append(GLib.Variant(
                        '(ia{sv}av)',
                        (stop_id, {
                            "label": GLib.Variant("s", "Stop"),
                            "enabled": GLib.Variant("b", True),
                            "icon-name": GLib.Variant("s", "media-playback-stop-symbolic"),
                        }, []),
                    ))

        if retired:
            sep_retired_id = next_id
            next_id += 1
            children.append(GLib.Variant(
                '(ia{sv}av)',
                (sep_retired_id, {"type": GLib.Variant("s", "separator")}, [])
            ))
            heading_id = next_id
            next_id += 1
            children.append(GLib.Variant(
                '(ia{sv}av)',
                (heading_id, {
                    "label": GLib.Variant("s", "stopping/stopped"),
                    "enabled": GLib.Variant("b", False),
                }, [])
            ))
            for entry in retired:
                s = entry["server"]
                row_id = next_id
                next_id += 1
                children.append(GLib.Variant(
                    '(ia{sv}av)',
                    (row_id, {
                        "label": GLib.Variant("s", self._server_menu_label(s, entry["state"])),
                        "enabled": GLib.Variant("b", False),
                    }, [])
                ))

        # Separator
        sep2_id = next_id
        next_id += 1
        v_sep2 = GLib.Variant('(ia{sv}av)', (sep2_id, {"type": GLib.Variant("s", "separator")}, []))
        children.append(v_sep2)

        # Gray section label, then the dashboard link
        settings_id = next_id
        next_id += 1
        v_settings = GLib.Variant(
            '(ia{sv}av)',
            (settings_id, {
                "label": GLib.Variant("s", "settings"),
                "enabled": GLib.Variant("b", False),
            }, [])
        )
        children.append(v_settings)

        dash_id = next_id
        next_id += 1
        self.action_callbacks[dash_id] = self._trigger_open_dashboard
        v_dash = GLib.Variant(
            '(ia{sv}av)',
            (dash_id, {"label": GLib.Variant("s", "Open Full Dashboard…"), "enabled": GLib.Variant("b", True)}, [])
        )
        children.append(v_dash)

        # Quit option if callback provided
        if self.on_quit:
            quit_id = next_id
            next_id += 1
            self.action_callbacks[quit_id] = lambda: GLib.idle_add(self.on_quit)
            v_quit = GLib.Variant(
                '(ia{sv}av)',
                (quit_id, {"label": GLib.Variant("s", "Quit WhatThePort"), "enabled": GLib.Variant("b", True)}, [])
            )
            children.append(v_quit)

        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

def run_tray():
    prefs = Preferences()
    monitor = ServerMonitor(prefs)
    monitor.start()

    loop = GLib.MainLoop()
    tray = StatusNotifierTray(monitor, on_quit=loop.quit)
    tray.start()
    loop.run()

if __name__ == "__main__":
    run_tray()
