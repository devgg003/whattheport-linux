"""Main GTK Application entry point with D-Bus toggle support and CSS styling."""
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

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._load_css()
        self.monitor.start()

    def do_activate(self):
        if not self.window:
            self.window = PopoverWindow(self.monitor)
            self.add_window(self.window)

        if self.window.is_visible():
            self.window.set_visible(False)
        else:
            self.window.present()

    def do_command_line(self, command_line):
        self.activate()
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
