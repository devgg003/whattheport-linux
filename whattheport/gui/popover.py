"""Popover window representing WhatThePort's main menu-bar panel."""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Pango
from typing import Optional, Set
from whattheport.engine.control import ProcessControl
from whattheport.engine.format import bytes_str, port_color, total_parts, percent
from whattheport.engine.monitor import ServerMonitor
from whattheport.engine.server import Server
from whattheport.gui.charts import MemoryBar
from whattheport.gui.detail import DetailView
from whattheport.gui.server_row import ServerRow
from whattheport.gui.settings import SettingsDialog

class PopoverWindow(Gtk.Window):
    def __init__(self, monitor: ServerMonitor):
        super().__init__()
        self.monitor = monitor
        self.set_default_size(380, 520)
        self.set_title("WhatThePort")
        self.add_css_class("whattheport-window")

        # Window state
        self.selected_server: Optional[Server] = None
        self.cleanup_mode = False
        self.cleanup_selected_ports: Set[int] = set()

        # Connect monitor listener
        self.monitor.add_listener(self._on_monitor_updated)

        self._build_ui()

    def _build_ui(self):
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        # Page 1: Servers List
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._build_header(self.list_box)
        self._build_server_list(self.list_box)
        self._build_footer(self.list_box)
        self.main_stack.add_named(self.list_box, "list")

        # Page 2: Detail container
        self.detail_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_stack.add_named(self.detail_container, "detail")

        self.set_child(self.main_stack)

    def _build_header(self, parent: Gtk.Box):
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_top(16)
        header.set_margin_bottom(12)

        # Title "Servers" centered
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_lbl = Gtk.Label(label="Servers")
        title_lbl.add_css_class("wtp-muted")
        title_lbl.set_hexpand(True)
        title_box.append(title_lbl)
        header.append(title_box)

        # Memory summary row: [4.8 GB] ... [CPU (servers) 6%]
        top_stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # Big memory label
        self.mem_num_lbl = Gtk.Label(label="0")
        self.mem_num_lbl.add_css_class("wtp-display-num")
        top_stats.append(self.mem_num_lbl)

        self.mem_unit_lbl = Gtk.Label(label="MB")
        self.mem_unit_lbl.add_css_class("wtp-display-unit")
        self.mem_unit_lbl.set_valign(Gtk.Align.END)
        self.mem_unit_lbl.set_margin_bottom(3)
        top_stats.append(self.mem_unit_lbl)

        # CPU label on right
        self.cpu_stat_lbl = Gtk.Label(label="CPU (servers) 0%")
        self.cpu_stat_lbl.add_css_class("wtp-muted")
        self.cpu_stat_lbl.set_hexpand(True)
        self.cpu_stat_lbl.set_xalign(1.0)
        self.cpu_stat_lbl.set_valign(Gtk.Align.END)
        self.cpu_stat_lbl.set_margin_bottom(3)
        top_stats.append(self.cpu_stat_lbl)

        header.append(top_stats)

        # Segmented Memory Bar
        self.memory_bar = MemoryBar()
        header.append(self.memory_bar)

        # Legend under memory bar: ■ Servers 4.8 GB  ― Other apps 7 GB  ― Free 4.2 of 16 GB
        self.legend_lbl = Gtk.Label(label="Servers 0 MB")
        self.legend_lbl.add_css_class("wtp-muted")
        self.legend_lbl.set_xalign(0.0)
        self.legend_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self.legend_lbl)

        parent.append(header)

    def _build_server_list(self, parent: Gtk.Box):
        # Scrolled window
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.rows_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.rows_container.set_margin_start(8)
        self.rows_container.set_margin_end(8)
        scroller.set_child(self.rows_container)

        parent.append(scroller)

    def _build_footer(self, parent: Gtk.Box):
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.add_css_class("wtp-footer")

        # Cleanup button
        self.cleanup_btn = Gtk.Button(label="Clean up")
        self.cleanup_btn.add_css_class("wtp-btn-action")
        self.cleanup_btn.connect("clicked", self._on_cleanup_clicked)
        footer.append(self.cleanup_btn)

        # If in cleanup mode: Stop Selected button
        self.stop_selected_btn = Gtk.Button(label="Stop selected")
        self.stop_selected_btn.add_css_class("wtp-btn-danger")
        self.stop_selected_btn.set_visible(False)
        self.stop_selected_btn.connect("clicked", self._on_stop_selected_clicked)
        footer.append(self.stop_selected_btn)

        # Settings gear icon
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.add_css_class("wtp-btn-action")
        settings_btn.set_hexpand(True)
        settings_btn.set_halign(Gtk.Align.END)
        settings_btn.connect("clicked", self._on_settings_clicked)
        footer.append(settings_btn)

        parent.append(footer)

    def _on_monitor_updated(self):
        GLib.idle_add(self._refresh_view)

    def _refresh_view(self):
        # 1. Update stats
        tot_mem = self.monitor.total_server_memory
        num, unit = total_parts(tot_mem)
        self.mem_num_lbl.set_text(num)
        self.mem_unit_lbl.set_text(unit)

        tot_cpu = self.monitor.total_server_cpu
        self.cpu_stat_lbl.set_text(f"CPU (servers) {percent(tot_cpu)}")

        # Memory bar segments
        sys_tot, sys_used, sys_avail = self.monitor.system_memory
        if sys_tot > 0:
            other_mem = max(0, sys_used - tot_mem)
            other_frac = other_mem / sys_tot
            segments = []
            for s in self.monitor.servers:
                color_idx = self.monitor.color_index(s.port)
                c_hex = port_color(color_idx, dark=True)
                frac = s.memory / sys_tot
                segments.append((frac, c_hex))
            self.memory_bar.set_data(segments, other_frac)

            # Update legend text
            free_str = bytes_str(sys_avail)
            tot_str = bytes_str(sys_tot)
            other_str = bytes_str(other_mem)
            self.legend_lbl.set_text(f"■ Servers {bytes_str(tot_mem)}  ― Other {other_str}  ― Free {free_str} of {tot_str}")

        # 2. Update Clean up button badge
        cand_count = self.monitor.suggested_cleanup_count
        if not self.cleanup_mode:
            self.cleanup_btn.set_label(f"Clean up {cand_count}" if cand_count > 0 else "Clean up")

        # 3. Rebuild server rows
        # Clear existing
        while child := self.rows_container.get_first_child():
            self.rows_container.remove(child)

        if not self.monitor.servers:
            empty_lbl = Gtk.Label(label="No dev servers running")
            empty_lbl.add_css_class("wtp-muted")
            empty_lbl.set_margin_top(40)
            empty_lbl.set_margin_bottom(40)
            self.rows_container.append(empty_lbl)
        else:
            thresh = self.monitor.prefs.threshold_bytes
            for s in self.monitor.servers:
                color_idx = self.monitor.color_index(s.port)
                c_hex = port_color(color_idx, dark=True)
                row = ServerRow(
                    server=s,
                    color_hex=c_hex,
                    alert_threshold=thresh,
                    on_select=self._open_detail,
                    on_stop=self._stop_server,
                    cleanup_mode=self.cleanup_mode,
                    on_toggle_cleanup=self._on_toggle_cleanup_port,
                    is_cleanup_selected=(s.port in self.cleanup_selected_ports)
                )
                self.rows_container.append(row)

        # 4. If detail view is currently active, update it
        if self.selected_server and self.main_stack.get_visible_child_name() == "detail":
            matched = next((s for s in self.monitor.servers if s.port == self.selected_server.port), None)
            if matched:
                self.selected_server = matched
                self._render_detail(matched)
            else:
                self.main_stack.set_visible_child_name("list")
                self.selected_server = None

        return False

    def _open_detail(self, server: Server):
        self.selected_server = server
        self._render_detail(server)
        self.main_stack.set_visible_child_name("detail")

    def _render_detail(self, server: Server):
        while child := self.detail_container.get_first_child():
            self.detail_container.remove(child)

        color_idx = self.monitor.color_index(server.port)
        c_hex = port_color(color_idx, dark=True)
        thresh = self.monitor.prefs.threshold_bytes
        gh_res = self.monitor.github.get(server.project.root, server.project.branch) if server.project else None

        detail = DetailView(
            server=server,
            color_hex=c_hex,
            alert_threshold=thresh,
            gh_result=gh_res,
            on_back=lambda: self.main_stack.set_visible_child_name("list"),
            on_refresh=self.monitor.scan
        )
        self.detail_container.append(detail)

    def _stop_server(self, server: Server):
        ProcessControl.stop(server, completion=self.monitor.scan)

    def _on_cleanup_clicked(self, btn):
        self.cleanup_mode = not self.cleanup_mode
        if self.cleanup_mode:
            # Preselect candidates that are not leaks
            candidates = self.monitor.cleanup_candidates
            self.cleanup_selected_ports = {c[0].port for c in candidates if not c[1].is_leak}
            self.cleanup_btn.set_label("Cancel")
            self.stop_selected_btn.set_visible(True)
            self._update_stop_selected_label()
        else:
            self.cleanup_btn.set_label("Clean up")
            self.stop_selected_btn.set_visible(False)
            self.cleanup_selected_ports.clear()
        self._refresh_view()

    def _on_toggle_cleanup_port(self, server: Server, active: bool):
        if active:
            self.cleanup_selected_ports.add(server.port)
        else:
            self.cleanup_selected_ports.discard(server.port)
        self._update_stop_selected_label()

    def _update_stop_selected_label(self):
        count = len(self.cleanup_selected_ports)
        self.stop_selected_btn.set_label(f"Stop ({count})" if count > 0 else "Stop")
        self.stop_selected_btn.set_sensitive(count > 0)

    def _on_stop_selected_clicked(self, btn):
        for s in self.monitor.servers:
            if s.port in self.cleanup_selected_ports:
                ProcessControl.stop(s)
        self.cleanup_mode = False
        self.cleanup_btn.set_label("Clean up")
        self.stop_selected_btn.set_visible(False)
        self.cleanup_selected_ports.clear()
        GLib.timeout_add(600, lambda: (self.monitor.scan(), False))

    def _on_settings_clicked(self, btn):
        dialog = SettingsDialog(self.monitor.prefs, on_saved=self.monitor.scan)
        dialog.set_transient_for(self)
        dialog.present()
