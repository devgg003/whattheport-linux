import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const WhatThePortIndicator = GObject.registerClass(
class WhatThePortIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'WhatThePort Indicator');

        this._box = new St.BoxLayout({
            style_class: 'panel-status-indicators-box wtp-indicator-box'
        });

        // Dot grid icon (⠿)
        this._iconLabel = new St.Label({
            text: '⠿',
            style_class: 'wtp-dotgrid',
            y_align: Clutter.ActorAlign.CENTER
        });
        this._box.add_child(this._iconLabel);

        // Active server count label
        this._countLabel = new St.Label({
            text: '',
            style_class: 'wtp-count-label',
            y_align: Clutter.ActorAlign.CENTER
        });
        this._box.add_child(this._countLabel);

        this.add_child(this._box);

        this.setMenu(new PopupMenu.PopupMenu(this, 0.0, St.Side.TOP));

        // GNOME Shell 50 click gesture support
        if (this._clickGesture) {
            this.remove_action(this._clickGesture);
        }
        this._clickGesture = new Clutter.ClickGesture();
        this._clickGesture.set_recognize_on_press(true);
        this._clickGesture.set_enabled(true);
        this._clickGesture.connect('recognize', () => {
            this._updateStatus();
            this.menu.toggle();
        });
        this.add_action(this._clickGesture);

        this._serversData = [];
        this._buildMenu();
        this._updateStatus();
        this._startPolling();
    }

    vfunc_event(event) {
        if (this.menu &&
            (event.type() === Clutter.EventType.TOUCH_BEGIN ||
             event.type() === Clutter.EventType.BUTTON_PRESS)
        ) {
            this._updateStatus();
            this.menu.toggle();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    _buildMenu() {
        this.menu.removeAll();

        // Header Item
        let headerItem = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
            style_class: 'wtp-popup-content'
        });

        let headerBox = new St.BoxLayout({
            vertical: true,
            x_expand: true
        });

        let titleLabel = new St.Label({
            text: 'WHATTHEPORT',
            style_class: 'wtp-popup-title'
        });
        headerBox.add_child(titleLabel);

        this._memTotalLabel = new St.Label({
            text: '0 MB',
            style_class: 'wtp-popup-mem'
        });
        headerBox.add_child(this._memTotalLabel);

        this._subSummaryLabel = new St.Label({
            text: 'No active dev servers',
            style_class: 'wtp-popup-servers'
        });
        headerBox.add_child(this._subSummaryLabel);

        headerItem.add_child(headerBox);
        this.menu.addMenuItem(headerItem);

        // Separator
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Dynamic server items container
        this._serverItemsSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._serverItemsSection);

        // Separator before actions
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Bottom action items
        let openAppItem = new PopupMenu.PopupMenuItem('Open Full Dashboard…');
        openAppItem.connect('activate', () => {
            try {
                let home = GLib.get_home_dir();
                Gio.Subprocess.new([`${home}/.local/bin/whattheport`], Gio.SubprocessFlags.NONE);
            } catch (e) {
                console.error(`WhatThePort launch error: ${e}`);
            }
        });
        this.menu.addMenuItem(openAppItem);
    }

    _updateStatus() {
        try {
            let home = GLib.get_home_dir();
            let proc = Gio.Subprocess.new(
                [`${home}/.local/bin/wtp`, '--json'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            );
            let [, out] = proc.communicate_utf8(null, null);
            if (proc.get_successful() && out) {
                let data = JSON.parse(out);
                this._serversData = data;

                let count = data.length;
                let attention = data.some(s => s.attention || s.leaking);

                if (count > 0) {
                    this._countLabel.set_text(`${count}`);
                    this._countLabel.show();
                } else {
                    this._countLabel.hide();
                }

                if (attention) {
                    this._iconLabel.style_class = 'wtp-dotgrid-amber';
                } else {
                    this._iconLabel.style_class = 'wtp-dotgrid';
                }

                this._renderMenuItems(data);
            }
        } catch (e) {
            console.error(`WhatThePort status error: ${e}`);
        }
    }

    _renderMenuItems(data) {
        let totalMemBytes = data.reduce((acc, s) => acc + (s.memory_bytes || 0), 0);
        let mb = totalMemBytes / (1024 * 1024);
        let memStr = mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
        this._memTotalLabel.set_text(memStr);

        let count = data.length;
        if (count === 0) {
            this._subSummaryLabel.set_text('No active dev servers');
        } else if (count === 1) {
            this._subSummaryLabel.set_text('1 active server');
        } else {
            this._subSummaryLabel.set_text(`${count} active servers`);
        }

        this._serverItemsSection.removeAll();

        if (count === 0) {
            let emptyItem = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
            emptyItem.add_child(new St.Label({ text: 'No servers listening on dev ports', style_class: 'wtp-row-sub' }));
            this._serverItemsSection.addMenuItem(emptyItem);
            return;
        }

        for (let s of data) {
            let item = new PopupMenu.PopupBaseMenuItem({
                reactive: true,
                can_focus: true
            });

            let rowBox = new St.BoxLayout({
                spacing: 8,
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER
            });

            // Port label
            let portLbl = new St.Label({
                text: `:${s.port}`,
                style_class: 'wtp-row-port',
                y_align: Clutter.ActorAlign.CENTER
            });
            rowBox.add_child(portLbl);

            // Center details
            let infoBox = new St.BoxLayout({
                vertical: true,
                x_expand: true
            });

            let nameLbl = new St.Label({
                text: s.name || 'Unknown',
                style_class: 'wtp-row-name'
            });
            infoBox.add_child(nameLbl);

            let subParts = [];
            if (s.agent) subParts.push(`✴ ${s.agent}`);
            else if (s.branch) subParts.push(s.branch);
            let mins = Math.max(1, Math.round((s.uptime_seconds || 0) / 60));
            subParts.push(mins >= 60 ? `${Math.floor(mins / 60)}h` : `${mins}m`);

            let subLbl = new St.Label({
                text: subParts.join(' · '),
                style_class: 'wtp-row-sub'
            });
            infoBox.add_child(subLbl);
            rowBox.add_child(infoBox);

            // Memory label on right
            let sMb = (s.memory_bytes || 0) / (1024 * 1024);
            let sMemStr = sMb >= 1024 ? `${(sMb / 1024).toFixed(1)} GB` : `${Math.round(sMb)} MB`;
            let memLbl = new St.Label({
                text: sMemStr,
                style_class: 'wtp-row-mem',
                y_align: Clutter.ActorAlign.CENTER
            });
            rowBox.add_child(memLbl);

            item.add_child(rowBox);

            // Clicking opens in browser
            item.connect('activate', () => {
                if (s.url && (s.url.startsWith('http://') || s.url.startsWith('https://'))) {
                    try {
                        Gio.AppInfo.launch_default_for_uri(s.url, null);
                    } catch (e) {
                        console.error(`Failed to open ${s.url}: ${e}`);
                    }
                }
            });

            this._serverItemsSection.addMenuItem(item);
        }
    }

    _startPolling() {
        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._updateStatus();
            return GLib.SOURCE_CONTINUE;
        });
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
        this._indicator = new WhatThePortIndicator();
        Main.panel.addToStatusArea('whattheport-indicator', this._indicator);
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}
