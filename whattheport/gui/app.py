"""Main GTK Application entry point with integrated top-bar StatusNotifierItem tray and CSS styling."""
import os
import sys
from typing import Optional
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Adw, Gio
from whattheport.engine.prefs import Preferences
from whattheport.engine.monitor import ServerMonitor
from whattheport.gui.popover import PopoverWindow
from whattheport.gui.tray_status_notifier import StatusNotifierTray

APP_ID = "dev.whattheport.WhatThePort"

class WhatThePortApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        )
        self.prefs = Preferences()
        self.monitor = ServerMonitor(self.prefs)
        self.window: Optional[PopoverWindow] = None
        self.tray: Optional[StatusNotifierTray] = None
        self._held = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._load_css()
        self.monitor.start()

        # Keep application alive in background so the top-bar tray icon persists
        if not self._held:
            self.hold()
            self._held = True

        # Start top-bar StatusNotifierItem indicator inside the application
        self.tray = StatusNotifierTray(
            self.monitor,
            on_open_dashboard=self.present_dashboard,
            on_quit=self.quit_app
        )
        self.tray.start()

    def _ensure_window(self):
        if not self.window:
            self.window = PopoverWindow(self.monitor)
            self.window.connect("close-request", self._on_window_close_request)
            self.add_window(self.window)

    def _on_window_close_request(self, window):
        # Hide window to top-bar tray instead of quitting the app
        window.set_visible(False)
        return True

    def present_dashboard(self):
        self._ensure_window()
        self.window.set_visible(True)
        self.window.present()
        return False

    def quit_app(self):
        self.monitor.stop()
        if self._held:
            self.release()
            self._held = False
        self.quit()
        return False

    def do_activate(self):
        self.present_dashboard()

    def do_command_line(self, command_line):
        args = command_line.get_arguments()
        if "--tray" in args:
            # Start quietly in the top bar without opening the dashboard window
            return 0
        self.present_dashboard()
        return 0

    def _load_css(self):
        css_provider = Gtk.CssProvider()
        css_path = os.path.join(os.path.dirname(__file__), "style.css")
        if os.path.isfile(css_path):
            css_provider.load_from_path(css_path)
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display,
                    css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )

def main():
    app = WhatThePortApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
