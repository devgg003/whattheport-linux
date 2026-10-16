"""DetailView widget showing full server info, charts, and process tree."""
import subprocess
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango
from typing import Callable, Optional
from whattheport.engine.control import ProcessControl
from whattheport.engine.format import bytes_str, duration, percent, PALETTE_AMBER
from whattheport.engine.github import GitHubResult
from whattheport.engine.server import Server
from whattheport.gui.charts import DetailChart

class DetailView(Gtk.Box):
    def __init__(self, server: Server, color_hex: str, alert_threshold: int, gh_result: Optional[GitHubResult], on_back: Callable[[], None], on_refresh: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.server = server
        self.color_hex = color_hex
        self.alert_threshold = alert_threshold
        self.gh_result = gh_result
        self.on_back = on_back
        self.on_refresh = on_refresh

        self.set_margin_start(14)
        self.set_margin_end(14)
        self.set_margin_top(12)
        self.set_margin_bottom(12)

        self._build_header()
        self._build_metadata()
        self._build_charts()
        self._build_processes()
        self._build_footer()

    def _build_header(self):
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Back button
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.add_css_class("wtp-btn-action")
        back_btn.connect("clicked", lambda b: self.on_back())
        header_box.append(back_btn)

        # Port and Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_box.set_hexpand(True)

        port_lbl = Gtk.Label()
        port_lbl.set_markup(f'<span foreground="{self.color_hex}" weight="bold" size="large">: {self.server.port}</span>')
        title_box.append(port_lbl)

        name_lbl = Gtk.Label(label=self.server.display_name)
        name_lbl.add_css_class("wtp-project-name")
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_box.append(name_lbl)

        header_box.append(title_box)

        # Right actions: Restart & Stop
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        restart_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        restart_btn.add_css_class("wtp-btn-action")
        restart_btn.set_tooltip_text("Restart server")
        restart_btn.connect("clicked", self._on_restart_clicked)
        actions_box.append(restart_btn)

        stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic")
        stop_btn.add_css_class("wtp-btn-action")
        stop_btn.set_tooltip_text("Stop server")
        stop_btn.connect("clicked", self._on_stop_clicked)
        actions_box.append(stop_btn)

        header_box.append(actions_box)
        self.append(header_box)

    def _build_metadata(self):
        meta_grid = Gtk.Grid()
        meta_grid.set_column_spacing(12)
        meta_grid.set_row_spacing(6)

        row = 0

        # Agent Session if any
        if self.server.agent:
            lbl_k = Gtk.Label(label="Session")
            lbl_k.add_css_class("wtp-muted")
            lbl_k.set_xalign(0.0)
            meta_grid.attach(lbl_k, 0, row, 1, 1)

            lbl_v = Gtk.Label(label=f"✴ {self.server.agent.title or self.server.agent.short_id}")
            lbl_v.add_css_class("wtp-subtext")
            lbl_v.set_xalign(0.0)
            lbl_v.set_ellipsize(Pango.EllipsizeMode.END)
            meta_grid.attach(lbl_v, 1, row, 1, 1)
            row += 1

        # Branch
        if self.server.project and self.server.project.branch:
            lbl_k = Gtk.Label(label="Branch")
            lbl_k.add_css_class("wtp-muted")
            lbl_k.set_xalign(0.0)
            meta_grid.attach(lbl_k, 0, row, 1, 1)

            lbl_v = Gtk.Label(label=self.server.project.branch)
            lbl_v.add_css_class("wtp-subtext")
            lbl_v.set_xalign(0.0)
            lbl_v.set_ellipsize(Pango.EllipsizeMode.END)
            meta_grid.attach(lbl_v, 1, row, 1, 1)
            row += 1

        # Uptime
        lbl_k = Gtk.Label(label="Uptime")
        lbl_k.add_css_class("wtp-muted")
        lbl_k.set_xalign(0.0)
        meta_grid.attach(lbl_k, 0, row, 1, 1)

        lbl_v = Gtk.Label(label=duration(self.server.uptime))
        lbl_v.add_css_class("wtp-subtext")
        lbl_v.set_xalign(0.0)
        meta_grid.attach(lbl_v, 1, row, 1, 1)
        row += 1

        # Command
        if self.server.command:
            lbl_k = Gtk.Label(label="Command")
            lbl_k.add_css_class("wtp-muted")
            lbl_k.set_xalign(0.0)
            meta_grid.attach(lbl_k, 0, row, 1, 1)

            lbl_v = Gtk.Label(label=self.server.command)
            lbl_v.add_css_class("wtp-muted-mono")
            lbl_v.set_xalign(0.0)
            lbl_v.set_ellipsize(Pango.EllipsizeMode.END)
            meta_grid.attach(lbl_v, 1, row, 1, 1)
            row += 1

        self.append(meta_grid)

    def _build_charts(self):
        # Memory Section
        mem_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mem_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        mem_lbl = Gtk.Label(label=f"Memory  {bytes_str(self.server.memory)}")
        mem_lbl.add_css_class("wtp-subtext")
        mem_hdr.append(mem_lbl)

        time_lbl = Gtk.Label(label="10 min")
        time_lbl.add_css_class("wtp-muted")
        time_lbl.set_hexpand(True)
        time_lbl.set_xalign(1.0)
        mem_hdr.append(time_lbl)
        mem_box.append(mem_hdr)

        mem_samples = [s.memory / (1024 * 1024) for s in self.server.history]
        thresh_mb = self.alert_threshold / (1024 * 1024)
        mem_color = f"#{PALETTE_AMBER[1]:06x}" if self.server.is_leaking() else self.color_hex

        mem_chart = DetailChart(is_cpu=False, color_hex=mem_color)
        mem_chart.set_data(mem_samples, threshold_val=thresh_mb)
        mem_box.append(mem_chart)
        self.append(mem_box)

        # CPU Section
        cpu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cpu_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        cpu_lbl = Gtk.Label(label=f"CPU  {percent(self.server.cpu)}")
        cpu_lbl.add_css_class("wtp-subtext")
        cpu_hdr.append(cpu_lbl)
        cpu_box.append(cpu_hdr)

        cpu_samples = [s.cpu for s in self.server.history]
        cpu_chart = DetailChart(is_cpu=True, color_hex=self.color_hex)
        cpu_chart.set_data(cpu_samples)
        cpu_box.append(cpu_chart)
        self.append(cpu_box)

    def _build_processes(self):
        proc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        proc_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        cnt = len(self.server.processes)
        proc_title = Gtk.Label(label=f"Processes ({cnt})")
        proc_title.add_css_class("wtp-muted")
        proc_hdr.append(proc_title)

        tot_mem = Gtk.Label(label=bytes_str(self.server.memory))
        tot_mem.add_css_class("wtp-muted-mono")
        tot_mem.set_hexpand(True)
        tot_mem.set_xalign(1.0)
        proc_hdr.append(tot_mem)
        proc_box.append(proc_hdr)

        for p in self.server.processes[:5]:
            p_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            indent = "  " * p.depth
            p_name = Gtk.Label(label=f"{indent}{p.name}")
            p_name.add_css_class("wtp-subtext")
            p_name.set_ellipsize(Pango.EllipsizeMode.END)
            p_row.append(p_name)

            p_stats = Gtk.Label(label=f"{bytes_str(p.memory)} · {percent(p.cpu)}")
            p_stats.add_css_class("wtp-muted-mono")
            p_stats.set_hexpand(True)
            p_stats.set_xalign(1.0)
            p_row.append(p_stats)
            proc_box.append(p_row)

        self.append(proc_box)

    def _build_footer(self):
        footer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer_box.set_margin_top(8)

        # Open in Browser button
        open_btn = Gtk.Button(label=f"Open localhost:{self.server.port}")
        open_btn.add_css_class("wtp-btn-primary")
        open_btn.set_hexpand(True)
        open_btn.connect("clicked", lambda b: self._open_url(self.server.url))
        footer_box.append(open_btn)

        # Preview button if Vercel deployment available
        if self.gh_result and self.gh_result.preview:
            prev_btn = Gtk.Button(label="▲ Preview")
            prev_btn.add_css_class("wtp-btn-action")
            prev_btn.connect("clicked", lambda b: self._open_url(self.gh_result.preview.url))
            footer_box.append(prev_btn)

        # PR button if pull request available
        if self.gh_result and self.gh_result.pull_request:
            pr_btn = Gtk.Button(label=f"PR #{self.gh_result.pull_request.number}")
            pr_btn.add_css_class("wtp-btn-action")
            pr_btn.connect("clicked", lambda b: self._open_url(self.gh_result.pull_request.url))
            footer_box.append(pr_btn)

        self.append(footer_box)

    @staticmethod
    def _open_url(url: str):
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            subprocess.run(["xdg-open", url], check=False)

    def _on_stop_clicked(self, btn):
        ProcessControl.stop(self.server, completion=self.on_refresh)
        self.on_back()

    def _on_restart_clicked(self, btn):
        ProcessControl.restart(self.server, completion=lambda ok: self.on_refresh())
