"""Settings dialog widget for WhatThePort configuration."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from typing import Callable
from whattheport.engine.prefs import Preferences

class SettingsDialog(Adw.PreferencesWindow):
    def __init__(self, prefs: Preferences, on_saved: Callable[[], None]):
        super().__init__()
        self.prefs = prefs
        self.on_saved = on_saved
        self.set_title("WhatThePort Settings")
        self.set_default_size(440, 520)

        # General Page
        page = Adw.PreferencesPage()
        page.set_title("General")
        page.set_icon_name("preferences-system-symbolic")

        # Port range group
        grp_ports = Adw.PreferencesGroup()
        grp_ports.set_title("Port Range")

        row_min = Adw.SpinRow.new_with_range(1024, 65535, 1)
        row_min.set_title("Minimum Port")
        row_min.set_value(self.prefs.min_port)
        row_min.connect("notify::value", lambda r, p: self._update_int("min_port", int(r.get_value())))
        grp_ports.add(row_min)

        row_max = Adw.SpinRow.new_with_range(1024, 65535, 1)
        row_max.set_title("Maximum Port")
        row_max.set_value(self.prefs.max_port)
        row_max.connect("notify::value", lambda r, p: self._update_int("max_port", int(r.get_value())))
        grp_ports.add(row_max)

        page.add(grp_ports)

        # Memory & Alerts Group
        grp_alerts = Adw.PreferencesGroup()
        grp_alerts.set_title("Alerts &amp; Memory")

        row_thresh = Adw.SpinRow.new_with_range(0.5, 32.0, 0.5)
        row_thresh.set_title("Memory Threshold (GB)")
        row_thresh.set_value(float(self.prefs.get("threshold_gb", 2.0)))
        row_thresh.connect("notify::value", lambda r, p: self._update_float("threshold_gb", float(r.get_value())))
        grp_alerts.add(row_thresh)

        row_leak = Adw.SwitchRow()
        row_leak.set_title("Leak Warnings")
        row_leak.set_subtitle("Alert when a server grows >500 MB in 10 minutes")
        row_leak.set_active(self.prefs.get("leak_warnings", True))
        row_leak.connect("notify::active", lambda r, p: self._update_bool("leak_warnings", r.get_active()))
        grp_alerts.add(row_leak)

        page.add(grp_alerts)

        # Clean Up Group
        grp_clean = Adw.PreferencesGroup()
        grp_clean.set_title("Clean Up")

        row_clean_mode = Adw.ComboRow()
        row_clean_mode.set_title("Clean Up Mode")
        model = Gtk.StringList.new(["Off", "Ask", "Automatic"])
        row_clean_mode.set_model(model)
        mode_idx = 0 if self.prefs.cleanup_mode == "off" else (2 if self.prefs.cleanup_mode == "automatic" else 1)
        row_clean_mode.set_selected(mode_idx)
        row_clean_mode.connect("notify::selected", self._on_cleanup_mode_changed)
        grp_clean.add(row_clean_mode)

        row_worktree = Adw.SwitchRow()
        row_worktree.set_title("Deleted Worktrees")
        row_worktree.set_subtitle("Suggest servers from deleted worktree folders")
        row_worktree.set_active(self.prefs.get("cleanup_deleted_worktree", True))
        row_worktree.connect("notify::active", lambda r, p: self._update_bool("cleanup_deleted_worktree", r.get_active()))
        grp_clean.add(row_worktree)

        page.add(grp_clean)

        # Integrations Group
        grp_int = Adw.PreferencesGroup()
        grp_int.set_title("Integrations")

        row_claude = Adw.SwitchRow()
        row_claude.set_title("Link Claude Code Sessions")
        row_claude.set_active(self.prefs.get("link_claude", True))
        row_claude.connect("notify::active", lambda r, p: self._update_bool("link_claude", r.get_active()))
        grp_int.add(row_claude)

        row_codex = Adw.SwitchRow()
        row_codex.set_title("Link Codex Sessions")
        row_codex.set_active(self.prefs.get("link_codex", True))
        row_codex.connect("notify::active", lambda r, p: self._update_bool("link_codex", r.get_active()))
        grp_int.add(row_codex)

        row_branch = Adw.SwitchRow()
        row_branch.set_title("Show Git Branches")
        row_branch.set_active(self.prefs.get("show_branches", True))
        row_branch.connect("notify::active", lambda r, p: self._update_bool("show_branches", r.get_active()))
        grp_int.add(row_branch)

        row_vercel = Adw.SwitchRow()
        row_vercel.set_title("Vercel Preview Links")
        row_vercel.set_subtitle("Requires GitHub CLI (gh)")
        row_vercel.set_active(self.prefs.get("vercel_previews", False))
        row_vercel.connect("notify::active", lambda r, p: self._update_bool("vercel_previews", r.get_active()))
        grp_int.add(row_vercel)

        row_gh_pr = Adw.SwitchRow()
        row_gh_pr.set_title("GitHub Pull Request Links")
        row_gh_pr.set_subtitle("Requires GitHub CLI (gh)")
        row_gh_pr.set_active(self.prefs.get("github_pull_requests", False))
        row_gh_pr.connect("notify::active", lambda r, p: self._update_bool("github_pull_requests", r.get_active()))
        grp_int.add(row_gh_pr)

        page.add(grp_int)

        self.add(page)

    def _update_int(self, key: str, val: int):
        self.prefs.set(key, val)
        self.on_saved()

    def _update_float(self, key: str, val: float):
        self.prefs.set(key, val)
        self.on_saved()

    def _update_bool(self, key: str, val: bool):
        self.prefs.set(key, val)
        self.on_saved()

    def _on_cleanup_mode_changed(self, row, pspec):
        sel = row.get_selected()
        modes = ["off", "ask", "automatic"]
        if 0 <= sel < len(modes):
            self.prefs.set("cleanup_mode", modes[sel])
            self.on_saved()
