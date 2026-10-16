"""ServerRow widget representing an item in the servers list."""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango
from typing import Callable, Optional
from whattheport.engine.format import bytes_str, duration, PALETTE_AMBER
from whattheport.engine.server import Server, ServerStatus
from whattheport.gui.charts import Sparkline

class ServerRow(Gtk.Box):
    def __init__(self, server: Server, color_hex: str, alert_threshold: int, on_select: Callable[[Server], None], on_stop: Callable[[Server], None], cleanup_mode: bool = False, on_toggle_cleanup: Optional[Callable[[Server, bool], None]] = None, is_cleanup_selected: bool = False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.server = server
        self.color_hex = color_hex
        self.on_select = on_select
        self.on_stop = on_stop
        self.cleanup_mode = cleanup_mode
        self.on_toggle_cleanup = on_toggle_cleanup

        self.add_css_class("wtp-server-row")

        # Checkbox if in cleanup mode
        if self.cleanup_mode:
            self.check = Gtk.CheckButton()
            self.check.set_active(is_cleanup_selected)
            self.check.connect("toggled", self._on_check_toggled)
            self.append(self.check)

        # Port column with colon e.g. ": 3000"
        self.port_label = Gtk.Label(label=f": {server.port}")
        self.port_label.add_css_class("wtp-port")
        self.port_label.set_xalign(0.0)
        self.port_label.set_width_chars(7)
        # Apply port color inline via markup
        self.port_label.set_markup(f'<span foreground="{color_hex}" weight="bold">: {server.port}</span>')
        self.append(self.port_label)

        # Center info: Project name / command, subtext: (agent / branch / uptime / leak)
        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        center_box.set_hexpand(True)

        name_text = server.display_name
        self.name_label = Gtk.Label(label=name_text)
        self.name_label.add_css_class("wtp-project-name")
        self.name_label.set_xalign(0.0)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        center_box.append(self.name_label)

        # Subtitle info
        sub_parts = []
        if server.agent:
            sub_parts.append(f"✴ {server.agent.title or server.agent.kind}")
        elif server.project and server.project.branch:
            sub_parts.append(server.project.branch)
        elif not server.cwd_exists:
            sub_parts.append("Worktree deleted")

        sub_parts.append(f"up {duration(server.uptime)}")
        sub_text = " · ".join(sub_parts)

        self.sub_label = Gtk.Label(label=sub_text)
        self.sub_label.add_css_class("wtp-subtext")
        self.sub_label.set_xalign(0.0)
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.END)

        if server.is_leaking():
            growth_mb = server.memory_growth / (1024 * 1024)
            amber_hex = f"#{PALETTE_AMBER[1]:06x}"
            self.sub_label.set_markup(f'<span foreground="{amber_hex}" weight="500">+{growth_mb:.0f} MB in 10 min</span>')
        else:
            self.sub_label.set_text(sub_text)

        center_box.append(self.sub_label)
        self.append(center_box)

        # Right side: Sparkline + Memory reading + Action button
        right_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right_box.set_valign(Gtk.Align.CENTER)

        # Sparkline
        samples = [s.memory / (1024 * 1024) for s in server.history]
        spark_color = f"#{PALETTE_AMBER[1]:06x}" if server.is_leaking() else color_hex
        self.sparkline = Sparkline(color_hex=spark_color, width=42, height=14)
        self.sparkline.set_points(samples)
        right_box.append(self.sparkline)

        # Memory text (amber if status is attention)
        mem_str = bytes_str(server.memory)
        self.mem_label = Gtk.Label(label=mem_str)
        self.mem_label.set_xalign(1.0)
        self.mem_label.set_width_chars(8)
        if server.status(alert_threshold) == ServerStatus.ATTENTION:
            amber_hex = f"#{PALETTE_AMBER[1]:06x}"
            self.mem_label.set_markup(f'<span foreground="{amber_hex}" weight="bold">{mem_str}</span>')
        else:
            self.mem_label.add_css_class("wtp-mem-text")
        right_box.append(self.mem_label)

        # Stop action button (subtle)
        self.stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic")
        self.stop_btn.add_css_class("wtp-btn-action")
        self.stop_btn.connect("clicked", self._on_stop_clicked)
        right_box.append(self.stop_btn)

        self.append(right_box)

        # Click gesture to open detail
        gesture = Gtk.GestureClick()
        gesture.connect("released", self._on_clicked)
        self.add_controller(gesture)

    def _on_check_toggled(self, btn):
        if self.on_toggle_cleanup:
            self.on_toggle_cleanup(self.server, btn.get_active())

    def _on_clicked(self, gesture, n_press, x, y):
        # Don't trigger if clicked on checkbox or stop button
        if not self.cleanup_mode:
            self.on_select(self.server)

    def _on_stop_clicked(self, btn):
        self.on_stop(self.server)
