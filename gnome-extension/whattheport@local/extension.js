import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'dev.whattheport.WhatThePort';
const GLANCE_PATH = '/dev/whattheport/Glance';
const GLANCE_IFACE = 'dev.whattheport.Glance';
const INDICATOR_ROLE = 'whattheport-indicator';

// Held so the shell does not collect the name-watch callbacks.
let panelController = null;

function fetchGlance() {
    try {
        const reply = Gio.DBus.session.call_sync(
            BUS_NAME,
            GLANCE_PATH,
            GLANCE_IFACE,
            'GetGlance',
            null,
            new GLib.VariantType('(s)'),
            Gio.DBusCallFlags.NONE,
            400,
            null
        );
        const [payload] = reply.deep_unpack();
        return JSON.parse(payload);
    } catch (e) {
        return null;
    }
}

function callApp(method) {
    Gio.DBus.session.call(
        BUS_NAME,
        GLANCE_PATH,
        GLANCE_IFACE,
        method,
        null,
        null,
        Gio.DBusCallFlags.NONE,
        2000,
        null,
        (conn, res) => {
            try {
                conn.call_finish(res);
            } catch (e) {
                console.error(`WhatThePort ${method} failed: ${e}`);
            }
        }
    );
}

function appIsRunning() {
    try {
        const reply = Gio.DBus.session.call_sync(
            'org.freedesktop.DBus',
            '/org/freedesktop/DBus',
            'org.freedesktop.DBus',
            'NameHasOwner',
            new GLib.Variant('(s)', [BUS_NAME]),
            new GLib.VariantType('(b)'),
            Gio.DBusCallFlags.NONE,
            200,
            null
        );
        return reply.deep_unpack()[0];
    } catch (e) {
        return false;
    }
}

function callServer(method, port) {
    Gio.DBus.session.call(
        BUS_NAME,
        GLANCE_PATH,
        GLANCE_IFACE,
        method,
        new GLib.Variant('(u)', [port]),
        null,
        Gio.DBusCallFlags.NONE,
        2000,
        null,
        (conn, res) => {
            try {
                conn.call_finish(res);
            } catch (e) {
                console.error(`WhatThePort ${method} :${port} failed: ${e}`);
            }
        }
    );
}

function findSniIcon() {
    const area = Main.panel.statusArea;
    for (const key of Object.keys(area)) {
        const actor = area[key];
        const indicator = actor && actor._indicator;
        if (!indicator)
            continue;
        let id = '';
        let title = '';
        try {
            id = indicator.id || '';
            title = indicator.title || '';
        } catch (e) {
            continue;
        }
        if (id === 'whattheport' || title.startsWith('WhatThePort'))
            return actor;
    }
    return null;
}

const WhatThePortIndicator = GObject.registerClass(
class WhatThePortIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'WhatThePort Indicator');

        this._box = new St.BoxLayout({
            style_class: 'panel-status-indicators-box wtp-indicator-box',
            orientation: Clutter.Orientation.HORIZONTAL,
        });

        this._iconLabel = new St.Label({
            text: '⠿',
            style_class: 'wtp-dotgrid',
            y_align: Clutter.ActorAlign.CENTER
        });
        this._box.add_child(this._iconLabel);

        this._countLabel = new St.Label({
            text: '',
            style_class: 'wtp-count-label',
            y_align: Clutter.ActorAlign.CENTER
        });
        this._box.add_child(this._countLabel);

        this.add_child(this._box);

        this._buildMenu();
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._refresh();
        });
        this._refresh();
        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _buildMenu() {
        this._summaryItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
            can_focus: false,
        });
        this.menu.addMenuItem(this._summaryItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._serverSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._serverSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const settingsLabel = new PopupMenu.PopupMenuItem('settings', {
            reactive: false,
            can_focus: false,
        });
        settingsLabel.label.add_style_class_name('wtp-menu-section');
        this.menu.addMenuItem(settingsLabel);

        const openAppItem = new PopupMenu.PopupMenuItem('Open Full Dashboard…');
        openAppItem.connect('activate', () => {
            this.menu.close();
            callApp('OpenDashboard');
        });
        this.menu.addMenuItem(openAppItem);

        const quitItem = new PopupMenu.PopupMenuItem('Quit WhatThePort');
        quitItem.connect('activate', () => {
            this.menu.close();
            if (panelController)
                panelController.requestQuit();
        });
        this.menu.addMenuItem(quitItem);
    }

    _refresh() {
        const data = fetchGlance();
        if (!data) {
            this._countLabel.hide();
            this._summaryItem.label.set_text('WhatThePort is not running');
            this._serverSection.removeAll();
            return;
        }

        const servers = data.servers || [];
        const retired = data.retired || [];
        if (servers.length > 0) {
            this._countLabel.set_text(`${servers.length}`);
            this._countLabel.show();
        } else {
            this._countLabel.hide();
        }
        this._iconLabel.style_class = servers.some(s => s.attention)
            ? 'wtp-dotgrid-amber'
            : 'wtp-dotgrid';
        this._summaryItem.label.set_text(
            `Servers: ${data.memory || '0 MB'} (${servers.length} active)`);

        const key = JSON.stringify([servers, retired]);
        if (key === this._renderedKey)
            return;
        this._renderedKey = key;
        this._renderServers(servers, retired);
    }

    _renderServers(servers, retired) {
        this._serverSection.removeAll();

        if (servers.length === 0 && retired.length === 0) {
            this._serverSection.addMenuItem(new PopupMenu.PopupMenuItem(
                'No dev servers listening', { reactive: false, can_focus: false }));
            return;
        }

        for (const s of servers)
            this._serverSection.addMenuItem(this._serverRow(s));

        if (retired.length > 0) {
            this._serverSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            const heading = new PopupMenu.PopupMenuItem('stopping/stopped', {
                reactive: false,
                can_focus: false,
            });
            heading.label.add_style_class_name('wtp-menu-section');
            this._serverSection.addMenuItem(heading);
            for (const r of retired) {
                this._serverSection.addMenuItem(new PopupMenu.PopupMenuItem(
                    `:${r.port}  ${r.name}  ${r.state}`,
                    { reactive: false, can_focus: false }));
            }
        }
    }

    _serverRow(s) {
        // activate: false keeps the row itself from being one big click target.
        const item = new PopupMenu.PopupBaseMenuItem({
            activate: false,
            can_focus: false,
            style_class: 'wtp-server-item',
        });
        item.orientation = Clutter.Orientation.HORIZONTAL;

        const row = new St.BoxLayout({
            style_class: 'wtp-server-row',
            orientation: Clutter.Orientation.HORIZONTAL,
            x_expand: true,
        });

        const nameBox = new St.BoxLayout({
            style_class: 'wtp-server-name-box',
            orientation: Clutter.Orientation.HORIZONTAL,
            x_expand: true,
        });
        const port = new St.Label({
            text: `:${s.port}`,
            style_class: 'wtp-row-port',
            y_align: Clutter.ActorAlign.CENTER,
        });
        if (s.color)
            port.set_style(`color: ${s.color};`);
        nameBox.add_child(port);
        nameBox.add_child(new St.Label({
            text: s.name || 'Unknown',
            style_class: 'wtp-row-name',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        }));
        nameBox.add_child(new St.Label({
            text: s.memory || '',
            style_class: s.attention ? 'wtp-row-mem-alert' : 'wtp-row-mem',
            y_align: Clutter.ActorAlign.CENTER,
        }));

        const openButton = new St.Button({
            child: nameBox,
            style_class: 'wtp-open-button',
            x_expand: true,
            can_focus: true,
            accessible_name: `Open :${s.port}`,
        });
        openButton.connect('clicked', () => {
            this.menu.close();
            callServer('OpenServer', s.port);
        });
        row.add_child(openButton);

        if (!s.protected) {
            const stopButton = new St.Button({
                child: new St.Icon({
                    icon_name: 'media-playback-stop-symbolic',
                    style_class: 'popup-menu-icon',
                }),
                style_class: 'wtp-stop-button',
                can_focus: true,
                y_align: Clutter.ActorAlign.CENTER,
                accessible_name: `Stop :${s.port}`,
            });
            stopButton.connect('clicked', () => {
                callServer('StopServer', s.port);
                GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
                    this._refresh();
                    return GLib.SOURCE_REMOVE;
                });
            });
            row.add_child(stopButton);
        }

        item.add_child(row);
        return item;
    }

    destroy() {
        if (this._timeoutId) {
            GLib.Source.remove(this._timeoutId);
            this._timeoutId = null;
        }
        super.destroy();
    }
});

export default class WhatThePortExtension extends Extension {
    enable() {
        // The icon exists only while WhatThePort is running. Quit removes it;
        // launching the app puts it back.
        panelController = this;
        this._indicator = null;
        this._sniWatchId = 0;
        this._destroyRole();
        // Keep these on the instance. GJS drops bus-watch callbacks that are
        // only passed inline, which left the icon stuck after the app quit.
        this._appearedHandler = () => this._syncPresence();
        this._vanishedHandler = () => this._syncPresence();
        this._nameWatchId = Gio.bus_watch_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            this._appearedHandler,
            this._vanishedHandler
        );
        this._presenceId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            this._syncPresence();
            return GLib.SOURCE_CONTINUE;
        });
        this._syncPresence();
    }

    requestQuit() {
        // Keep the icon from popping back while the process is exiting.
        this._quitRequested = true;
        this.hideIndicator();
        callApp('Quit');
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2500, () => {
            if (this._quitRequested && appIsRunning())
                this._quitRequested = false;
            this._syncPresence();
            return GLib.SOURCE_REMOVE;
        });
    }

    _syncPresence() {
        if (!appIsRunning()) {
            this._quitRequested = false;
            this.hideIndicator();
            return;
        }
        if (this._quitRequested)
            return;
        this.showIndicator();
    }

    showIndicator() {
        if (!this._indicator && !Main.panel.statusArea[INDICATOR_ROLE]) {
            // The app's StatusNotifier menu gives each entry a single click, so a
            // separate stop button on the same row only exists in this menu.
            this._indicator = new WhatThePortIndicator();
            Main.panel.addToStatusArea(INDICATOR_ROLE, this._indicator);
        } else if (!this._indicator) {
            this._indicator = Main.panel.statusArea[INDICATOR_ROLE];
        }
        if (!this._sniWatchId) {
            this._sniWatchId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                this._hideSni();
                return GLib.SOURCE_CONTINUE;
            });
        }
        this._hideSni();
    }

    hideIndicator() {
        if (this._sniWatchId) {
            GLib.source_remove(this._sniWatchId);
            this._sniWatchId = 0;
        }
        this._destroyRole();
    }

    _destroyRole() {
        const indicator = this._indicator || Main.panel.statusArea[INDICATOR_ROLE];
        this._indicator = null;
        if (!indicator)
            return;
        try {
            indicator.destroy();
        } catch (e) {
            // Already removed with the panel.
        }
    }

    _hideSni() {
        let sni = null;
        try {
            sni = findSniIcon();
            if (sni && sni.visible)
                sni.hide();
        } catch (e) {
            // The AppIndicator actor is disposed when the app quits.
        }
    }

    disable() {
        if (this._presenceId) {
            GLib.source_remove(this._presenceId);
            this._presenceId = 0;
        }
        if (this._nameWatchId) {
            Gio.bus_unwatch_name(this._nameWatchId);
            this._nameWatchId = 0;
        }
        this.hideIndicator();
        if (panelController === this)
            panelController = null;
    }
}
