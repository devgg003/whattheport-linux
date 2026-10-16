"""FreeDesktop StatusNotifierItem (SNI) + Ayatana Label + DBusMenu implementation for WhatThePort."""
import os
import subprocess
from typing import Callable, Optional
import gi
gi.require_version('Gio', '2.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gio, GLib

from whattheport.engine.format import bytes_str
from whattheport.engine.monitor import ServerMonitor
from whattheport.engine.prefs import Preferences

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
        self.revision = 1
        self.action_callbacks = {}
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
            tot_mem = self.monitor.total_server_memory
            title = f"WhatThePort ({self._current_label()})"
            sub = f"RAM: {bytes_str(tot_mem)}"
            return GLib.Variant("(sa(ii)ss)", ("network-server", [], title, sub))
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
                cb = self.action_callbacks[item_id]
                cb()
            invocation.return_value(None)
        elif method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([],)))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))

    def _build_menu_layout(self):
        self.action_callbacks.clear()

        children = []
        next_id = 1

        servers = self.monitor.servers
        tot_mem = self.monitor.total_server_memory

        # Header item: Servers summary
        hdr_id = next_id
        next_id += 1
        summary_text = f"Servers: {bytes_str(tot_mem)} ({len(servers)} active)"
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

        # Server items
        if not servers:
            empty_id = next_id
            next_id += 1
            v_empty = GLib.Variant(
                '(ia{sv}av)',
                (empty_id, {"label": GLib.Variant("s", "No dev servers listening"), "enabled": GLib.Variant("b", False)}, [])
            )
            children.append(v_empty)
        else:
            for s in servers:
                s_id = next_id
                next_id += 1
                sub = f" ({s.project.branch})" if s.project and s.project.branch else ""
                label = f":{s.port}  {s.display_name}{sub}  ―  {bytes_str(s.memory)}"
                s_props = {
                    "label": GLib.Variant("s", label),
                    "enabled": GLib.Variant("b", True),
                }

                url = s.url
                self.action_callbacks[s_id] = lambda u=url: subprocess.Popen(["xdg-open", u])
                v_s = GLib.Variant('(ia{sv}av)', (s_id, s_props, []))
                children.append(v_s)

        # Separator
        sep2_id = next_id
        next_id += 1
        v_sep2 = GLib.Variant('(ia{sv}av)', (sep2_id, {"type": GLib.Variant("s", "separator")}, []))
        children.append(v_sep2)

        # Open Full Dashboard
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
