#!/usr/bin/env python3
"""Session-bus org.freedesktop.Notifications service that records every Notify call.

The wtp daemon sends leak and cleanup alerts here. Each notification is appended
as one JSON object per line to the path given on the command line.
"""
import json
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib


class Notifications(dbus.service.Object):
    def __init__(self, bus, log_path):
        # Object stores the BusName on self and keeps the well-known name claimed.
        bus_name = dbus.service.BusName("org.freedesktop.Notifications", bus)
        super().__init__(bus, "/org/freedesktop/Notifications", bus_name)
        self.log_path = log_path
        self.next_id = 1

    @dbus.service.method(
        "org.freedesktop.Notifications",
        in_signature="susssasa{sv}i",
        out_signature="u",
    )
    def Notify(self, app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout):
        nid = self.next_id
        self.next_id += 1
        record = {
            "id": nid,
            "app_name": str(app_name),
            "replaces_id": int(replaces_id),
            "icon": str(app_icon),
            "summary": str(summary),
            "body": str(body),
            "actions": [str(a) for a in actions],
            "expire_timeout": int(expire_timeout),
        }
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
        return dbus.UInt32(nid)

    @dbus.service.method("org.freedesktop.Notifications", in_signature="", out_signature="as")
    def GetCapabilities(self):
        return ["actions", "body"]

    @dbus.service.method("org.freedesktop.Notifications", in_signature="", out_signature="ssss")
    def GetServerInformation(self):
        return ("wtp-e2e-notifyd", "whattheport", "0.1", "1.2")

    @dbus.service.method("org.freedesktop.Notifications", in_signature="u", out_signature="")
    def CloseNotification(self, notification_id):
        return None


def main():
    log_path = sys.argv[1]
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    # Hold this reference for the life of the process. BusName releases the
    # well-known name as soon as it is garbage-collected.
    service = Notifications(bus, log_path)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ready": True, "unique": bus.get_unique_name()}) + "\n")
        fh.flush()
    GLib.MainLoop().run()
    # Referenced so the name owner is not collected while the loop runs.
    del service


if __name__ == "__main__":
    main()
