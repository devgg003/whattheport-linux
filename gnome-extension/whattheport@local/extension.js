import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'dev.whattheport.WhatThePort';
const OBJECT_PATH = '/dev/whattheport/Daemon';
const IFACE = 'dev.whattheport.Daemon1';

const BAR_WIDTH = 320;
const THRESHOLDS = [1, 2, 4, 8];
const CLEANUP_MODES = [['off', 'Off'], ['ask', 'Ask first'], ['automatic', 'Automatic']];
const TOGGLES = [
    ['leak_warnings', 'Memory and leak alerts'],
    ['show_branches', 'Show git branches'],
    ['link_claude', 'Link Claude Code sessions'],
    ['link_codex', 'Link Codex sessions'],
    ['github_pull_requests', 'Look up GitHub pull requests'],
    ['vercel_previews', 'Look up Vercel previews'],
    ['launch_at_login', 'Launch at login'],
];

function markEstimated(text, estimated) {
    if (!text || !estimated || text.startsWith('~'))
        return text;
    return `~${text}`;
}

function markSession(agent, estimated) {
    if (!agent || !estimated)
        return agent;
    const sep = agent.indexOf(': ');
    if (sep === -1)
        return markEstimated(agent, true);
    const title = agent.slice(sep + 2);
    if (title.startsWith('~'))
        return agent;
    return `${agent.slice(0, sep + 2)}~${title}`;
}

function call(method, params = null, onReply = null) {
    Gio.DBus.session.call(
        BUS_NAME, OBJECT_PATH, IFACE, method, params, null,
        Gio.DBusCallFlags.NO_AUTO_START, 5000, null,
        (conn, res) => {
            try {
                const reply = conn.call_finish(res);
                onReply?.(reply);
            } catch (e) {
                console.warn(`WhatThePort: ${method} failed: ${e.message}`);
            }
        });
}

const portCall = (method, port) => call(method, new GLib.Variant('(u)', [port]));
const setPref = (key, value) =>
    call('SetPref', new GLib.Variant('(ss)', [key, JSON.stringify(value)]));

// St.BoxLayout gained `orientation` in GNOME 48 and deprecated `vertical`.
function box(vertical, params = {}) {
    const b = new St.BoxLayout(params);
    if ('orientation' in b)
        b.orientation = vertical ? Clutter.Orientation.VERTICAL : Clutter.Orientation.HORIZONTAL;
    else
        b.vertical = vertical;
    return b;
}

function label(text, style, params = {}) {
    const l = new St.Label({text: text ?? '', style_class: style, y_align: Clutter.ActorAlign.CENTER, ...params});
    l.clutter_text.ellipsize = Pango.EllipsizeMode.END;
    return l;
}

function hexToRgb(hex) {
    const n = parseInt((hex || '#6EC7ED').slice(1), 16);
    return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

function iconButton(iconName, style, accessibleName, onClick) {
    const b = new St.Button({
        style_class: `wtp-icon-button ${style}`,
        child: new St.Icon({icon_name: iconName, icon_size: 14}),
        can_focus: true,
        track_hover: true,
        y_align: Clutter.ActorAlign.CENTER,
        accessible_name: accessibleName,
    });
    b.connect('clicked', onClick);
    return b;
}

function textButton(text, style, onClick) {
    const b = new St.Button({label: text, style_class: `wtp-text-button ${style}`, can_focus: true});
    b.connect('clicked', onClick);
    return b;
}

/** One server: a compact header row that expands in place into details and actions. */
const ServerRow = GObject.registerClass(
class ServerRow extends PopupMenu.PopupSubMenuMenuItem {
    _init(indicator) {
        super._init('', false);
        this._indicator = indicator;
        this.add_style_class_name('wtp-server-item');
        this.label.hide();
        const expander = this.label.get_next_sibling();
        if (expander)
            expander.x_expand = false;

        this._port = label('', 'wtp-row-port');
        this._name = label('', 'wtp-row-name');
        this._subtitle = label('', 'wtp-row-sub');
        const text = box(true, {x_expand: true, style_class: 'wtp-row-text'});
        text.add_child(this._name);
        text.add_child(this._subtitle);

        this._spark = new St.DrawingArea({style_class: 'wtp-spark', y_align: Clutter.ActorAlign.CENTER});
        this._spark.connect('repaint', area => this._drawSpark(area));
        this._memory = label('', 'wtp-row-mem', {x_align: Clutter.ActorAlign.END});

        this._open = iconButton('web-browser-symbolic', 'wtp-open-button', 'Open in browser', () => {
            this._indicator.menu.close();
            call('Open', new GLib.Variant('(us)', [this.s.port, 'local']));
        });
        this._stop = iconButton('media-playback-stop-symbolic', 'wtp-stop-button', 'Stop server',
            () => portCall('Stop', this.s.port));

        const row = box(false, {x_expand: true, style_class: 'wtp-row'});
        for (const child of [this._port, text, this._spark, this._memory, this._open, this._stop])
            row.add_child(child);
        this.insert_child_above(row, this.label);

        this._details = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false, style_class: 'wtp-details-item'});
        this._detailsBox = box(true, {x_expand: true, style_class: 'wtp-details'});
        this._details.add_child(this._detailsBox);
        this.menu.addMenuItem(this._details);

        this._actions = new PopupMenu.PopupBaseMenuItem({activate: false, hover: false, can_focus: false, style_class: 'wtp-actions-item'});
        this._actionsBox = box(false, {x_expand: true, style_class: 'wtp-actions'});
        this._actions.add_child(this._actionsBox);
        this.menu.addMenuItem(this._actions);
    }

    activate(event) {
        // The row's click gesture also sees clicks on its own buttons.
        if (this._open.hover || this._stop.hover)
            return;
        super.activate(event);
    }

    update(s) {
        const prev = this.s;
        this.s = s;
        this._port.text = `:${s.port}`;
        this._port.set_style(`color: ${s.color};`);
        this._name.text = s.name;
        const subtitle = markEstimated(s.subtitle, s.session_estimated);
        this._subtitle.text = s.cleanup ? `${subtitle} · ${s.cleanup}` : subtitle;
        this._memory.text = s.memory;
        this._memory.style_class = s.attention ? 'wtp-row-mem wtp-alert' : 'wtp-row-mem';
        this._stop.visible = !s.protected;
        this._spark.queue_repaint();

        const detailKey = JSON.stringify([s.framework, s.agent, s.session_estimated, s.branch, s.uptime, s.connections, s.command,
            s.directory, s.cpu, s.growth, s.cleanup, s.processes]);
        if (!prev || detailKey !== this._detailKey) {
            this._detailKey = detailKey;
            this._renderDetails(s);
        }
        const actionKey = JSON.stringify([s.can_restart, s.protected, s.attention, s.snoozed, s.pull_request, s.preview]);
        if (!prev || actionKey !== this._actionKey) {
            this._actionKey = actionKey;
            this._renderActions(s);
        }
    }

    _renderDetails(s) {
        this._detailsBox.destroy_all_children();
        const line = (key, value, style = '') => {
            if (!value)
                return;
            const l = box(false, {style_class: 'wtp-detail-line'});
            l.add_child(label(key, 'wtp-detail-key'));
            l.add_child(label(value, `wtp-detail-value ${style}`, {x_expand: true}));
            this._detailsBox.add_child(l);
        };
        line('Framework', s.framework);
        line('Session', markSession(s.agent, s.session_estimated));
        line('Branch', s.branch);
        line('Uptime', `${s.uptime}${s.connections ? ` · ${s.connections} connection${s.connections === 1 ? '' : 's'}` : ''}`);
        line('Command', s.command, 'wtp-mono');
        line('Folder', s.directory, 'wtp-mono');
        line('CPU', `${s.cpu}${s.growth ? ` · ${s.growth}` : ''}`);
        line('Clean up', s.cleanup, 'wtp-alert');

        if (s.processes.length > 1) {
            this._detailsBox.add_child(label(`Processes (${s.processes.length})`, 'wtp-detail-heading'));
            for (const p of s.processes) {
                const l = box(false, {style_class: 'wtp-proc-line'});
                l.add_child(label(`${'  '.repeat(p.depth)}${p.name}`, 'wtp-proc-name wtp-mono', {x_expand: true}));
                l.add_child(label(`${p.memory} · ${p.cpu}`, 'wtp-proc-stats wtp-mono'));
                this._detailsBox.add_child(l);
            }
        }
    }

    _renderActions(s) {
        this._actionsBox.destroy_all_children();
        const add = (text, onClick, style = '') => this._actionsBox.add_child(textButton(text, style, onClick));
        const open = kind => () => {
            this._indicator.menu.close();
            call('Open', new GLib.Variant('(us)', [s.port, kind]));
        };
        add('Open', open('local'));
        if (s.pull_request)
            add(`PR #${s.pull_request.number}`, open('pr'));
        if (s.preview)
            add(`Preview (${s.preview.state})`, open('preview'));
        if (s.can_restart)
            add('Restart', () => portCall('Restart', s.port));
        if (s.attention && !s.snoozed)
            add('Mute alerts', () => portCall('Snooze', s.port));
        if (!s.protected)
            add('Stop', () => portCall('Stop', s.port), 'wtp-danger');
    }

    _drawSpark(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const pts = this.s?.spark ?? [];
        if (pts.length >= 2) {
            const [r, g, b] = hexToRgb(this.s.color);
            cr.setLineWidth(1.5);
            cr.setSourceRGBA(r, g, b, 0.95);
            pts.forEach((v, i) => {
                const x = 1 + i / (pts.length - 1) * (w - 2);
                const y = h - 1.5 - v * (h - 3);
                if (i === 0)
                    cr.moveTo(x, y);
                else
                    cr.lineTo(x, y);
            });
            cr.stroke();
        }
        cr.$dispose();
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extensionPath) {
        super._init(0.5, 'WhatThePort');
        this.menu.actor.add_style_class_name('wtp-menu');

        const panelBox = box(false, {style_class: 'panel-status-indicators-box wtp-panel'});
        this._icon = new St.Icon({
            gicon: Gio.icon_new_for_string(`${extensionPath}/icons/whattheport-symbolic.svg`),
            style_class: 'system-status-icon wtp-panel-icon',
        });
        this._count = label('', 'wtp-panel-count');
        panelBox.add_child(this._icon);
        panelBox.add_child(this._count);
        this.add_child(panelBox);

        this._buildMenu();
        this._rows = new Map();

        this._signalId = Gio.DBus.session.signal_subscribe(
            BUS_NAME, IFACE, 'StateChanged', OBJECT_PATH, null, Gio.DBusSignalFlags.NONE,
            (_c, _s, _p, _i, _n, params) => this._apply(params.deep_unpack()[0]));
        call('GetState', null, reply => this._apply(reply.deep_unpack()[0]));
    }

    _buildMenu() {
        // Memory summary: headline, segmented bar coloured by port, legend + hover breakdown.
        const summary = new PopupMenu.PopupBaseMenuItem({activate: false, hover: false, can_focus: false, style_class: 'wtp-summary-item'});
        const summaryBox = box(true, {x_expand: true, style_class: 'wtp-summary'});
        const head = box(false, {style_class: 'wtp-summary-head'});
        this._total = label('', 'wtp-summary-total');
        this._totalCaption = label('', 'wtp-summary-caption', {x_expand: true});
        this._cpu = label('', 'wtp-summary-caption');
        head.add_child(this._total);
        head.add_child(this._totalCaption);
        head.add_child(this._cpu);
        this._bar = box(false, {
            style_class: 'wtp-membar',
            width: BAR_WIDTH,
            reactive: true,
            track_hover: true,
        });
        this._bar.connect('notify::hover', () => this._syncBarHover());
        this._legend = label('', 'wtp-summary-legend');
        this._barTooltip = box(true, {style_class: 'wtp-bar-tooltip', visible: false});
        summaryBox.add_child(head);
        summaryBox.add_child(this._bar);
        summaryBox.add_child(this._legend);
        summaryBox.add_child(this._barTooltip);
        summary.add_child(summaryBox);
        this.menu.addMenuItem(summary);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._serverSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._serverSection);
        this._empty = new PopupMenu.PopupMenuItem('No dev servers listening', {reactive: false, can_focus: false});
        this._empty.label.add_style_class_name('wtp-dim');
        this.menu.addMenuItem(this._empty);
        this._retiredSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._retiredSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._cleanup = new PopupMenu.PopupImageMenuItem('', 'edit-clear-all-symbolic');
        this._cleanup.connect('activate', () => call('CleanUp'));
        this.menu.addMenuItem(this._cleanup);

        this._settings = new PopupMenu.PopupSubMenuMenuItem('Settings', true);
        this._settings.icon.icon_name = 'emblem-system-symbolic';
        const heading = text => {
            const item = new PopupMenu.PopupMenuItem(text, {reactive: false, can_focus: false});
            item.label.add_style_class_name('wtp-section-heading');
            this._settings.menu.addMenuItem(item);
        };
        heading('Memory alert above');
        this._thresholdItems = THRESHOLDS.map(gb => {
            const item = new PopupMenu.PopupMenuItem(`${gb} GB`);
            item.connect('activate', () => setPref('threshold_gb', gb));
            this._settings.menu.addMenuItem(item);
            return [gb, item];
        });
        heading('Clean up idle servers');
        this._cleanupItems = CLEANUP_MODES.map(([mode, text]) => {
            const item = new PopupMenu.PopupMenuItem(text);
            item.connect('activate', () => setPref('cleanup_mode', mode));
            this._settings.menu.addMenuItem(item);
            return [mode, item];
        });
        this._settings.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._toggles = TOGGLES.map(([key, text]) => {
            const item = new PopupMenu.PopupSwitchMenuItem(text, false);
            item.connect('toggled', (_item, state) => setPref(key, state));
            this._settings.menu.addMenuItem(item);
            return [key, item];
        });
        this.menu.addMenuItem(this._settings);

        const quit = new PopupMenu.PopupImageMenuItem('Quit WhatThePort', 'application-exit-symbolic');
        quit.connect('activate', () => call('Quit'));
        this.menu.addMenuItem(quit);
    }

    _apply(json) {
        let state;
        try {
            state = JSON.parse(json);
        } catch (e) {
            return;
        }
        this._state = state;
        this._count.text = state.count > 0 ? `${state.count}` : '';
        this._count.visible = state.count > 0;
        if (state.attention)
            this._icon.add_style_class_name('wtp-alert');
        else
            this._icon.remove_style_class_name('wtp-alert');

        this._applyServers(state.servers);
        this._applySummary(state.summary, state.servers, state.count);

        this._retiredSection.removeAll();
        for (const r of state.retired) {
            const item = new PopupMenu.PopupMenuItem(`:${r.port}  ${r.name}  —  ${r.state}…`, {reactive: false, can_focus: false});
            item.label.add_style_class_name('wtp-dim');
            this._retiredSection.addMenuItem(item);
        }
        this._empty.visible = state.servers.length === 0 && state.retired.length === 0;

        this._cleanup.visible = !!state.cleanup;
        if (state.cleanup)
            this._cleanup.label.text = state.cleanup.label;

        const p = state.prefs;
        for (const [gb, item] of this._thresholdItems)
            item.setOrnament(gb === p.threshold_gb ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        for (const [mode, item] of this._cleanupItems)
            item.setOrnament(mode === p.cleanup_mode ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        for (const [key, item] of this._toggles) {
            if (item.state !== p[key])
                item.setToggleState(p[key]);
        }
    }

    _fmtPct(fraction) {
        const pct = (fraction ?? 0) * 100;
        if (pct <= 0)
            return '0%';
        if (pct < 0.1)
            return '<0.1%';
        if (pct < 10)
            return `${pct.toFixed(1)}%`;
        return `${Math.round(pct)}%`;
    }

    _applySummary(sum, servers, count) {
        this._summaryData = {sum, servers: servers ?? [], count};
        this._barSegments = [];
        this._tooltipRows = [];

        this._bar.destroy_all_children();
        this._barTooltip.destroy_all_children();

        const srvList = servers ?? [];
        const minServerPx = srvList.length > 0 ? Math.min(10, Math.floor((BAR_WIDTH * 0.6) / srvList.length)) : 10;
        let usedPx = 0;

        const addSegment = (width, style, color, meta) => {
            if (width < 1)
                return null;
            const w = new St.Button({
                style_class: style,
                width,
                y_expand: true,
                reactive: true,
                track_hover: true,
                can_focus: false,
            });
            if (color)
                w.set_style(`background-color: ${color};`);
            w.connect('notify::hover', () => this._syncBarHover());
            if (meta?.key) {
                w.connect('clicked', () => {
                    const row = this._rows.get(meta.key);
                    row?.menu.toggle();
                });
            }
            this._bar.add_child(w);
            this._barSegments.push({actor: w, meta});
            return w;
        };

        sum.segments.forEach((seg, i) => {
            const srv = srvList[i];
            const color = srv?.color ?? seg.color;
            const width = Math.max(Math.round(seg.fraction * BAR_WIDTH), minServerPx);
            usedPx += width;
            const meta = {
                kind: 'server',
                index: i,
                key: srv?.key,
                port: seg.port ?? srv?.port ?? '',
                name: seg.name ?? srv?.name ?? 'server',
                memory: seg.memory ?? srv?.memory ?? '',
                cpu: srv?.cpu ?? sum.cpu,
                subtitle: markEstimated(srv?.subtitle ?? '', srv?.session_estimated),
                color,
                percent: seg.percent ?? this._fmtPct(seg.fraction),
            };
            addSegment(width, 'wtp-bar-segment', color, meta);

            // Build corresponding row in the hover breakdown tooltip
            const tipRow = box(false, {style_class: 'wtp-bar-tip-row'});
            const dot = label('●', 'wtp-bar-tip-dot');
            dot.set_style(`color: ${color};`);
            const portLbl = label(`:${meta.port}`, 'wtp-bar-tip-port wtp-mono');
            portLbl.set_style(`color: ${color};`);
            const nameLbl = label(meta.name, 'wtp-bar-tip-name', {x_expand: true});
            const statsLbl = label(`${meta.memory} (${meta.percent})`, 'wtp-bar-tip-stats wtp-mono');
            tipRow.add_child(dot);
            tipRow.add_child(portLbl);
            tipRow.add_child(nameLbl);
            tipRow.add_child(statsLbl);
            this._barTooltip.add_child(tipRow);
            this._tooltipRows.push({actor: tipRow, key: meta.key});
        });

        const remainingPx = Math.max(0, BAR_WIDTH - usedPx);
        const freeFraction = Math.max(0, 1 - sum.segments.reduce((a, s) => a + s.fraction, 0) - sum.other_fraction);
        const otherAndFree = sum.other_fraction + freeFraction;
        const otherWidth = otherAndFree > 0
            ? Math.round((sum.other_fraction / otherAndFree) * remainingPx)
            : 0;
        const freeWidth = Math.max(0, remainingPx - otherWidth);

        if (otherWidth > 0) {
            addSegment(otherWidth, 'wtp-bar-other', null, {
                kind: 'other',
                memory: sum.other_memory,
                percent: this._fmtPct(sum.other_fraction),
            });
        }
        if (freeWidth > 0) {
            addSegment(freeWidth, 'wtp-bar-free', null, {
                kind: 'free',
                memory: sum.free_memory,
                percent: this._fmtPct(freeFraction),
            });
        }

        this._syncBarHover();
    }

    _syncBarHover() {
        if (!this._summaryData)
            return;
        const {sum, servers, count} = this._summaryData;
        const hovered = this._barSegments?.find(s => s.actor.hover);
        const barHovered = this._bar.hover || !!hovered;

        this._barTooltip.visible = barHovered && servers.length > 0;

        for (const [, row] of this._rows)
            row.remove_style_class_name('wtp-server-item-highlight');

        if (!barHovered) {
            this._total.text = sum.servers_memory;
            this._total.set_style(null);
            this._totalCaption.text = `in ${count} server${count === 1 ? '' : 's'}`;
            this._cpu.text = `CPU ${sum.cpu}`;
            this._legend.text = `Other ${sum.other_memory}  ·  Free ${sum.free_memory}`;
            for (const s of this._barSegments ?? [])
                s.actor.opacity = 255;
            for (const r of this._tooltipRows ?? [])
                r.actor.remove_style_class_name('wtp-bar-tip-row-active');
            return;
        }

        const activeMeta = hovered?.meta;
        for (const s of this._barSegments ?? [])
            s.actor.opacity = (!activeMeta || s === hovered) ? 255 : 130;

        for (const r of this._tooltipRows ?? []) {
            if (activeMeta?.kind === 'server' && r.key === activeMeta.key)
                r.actor.add_style_class_name('wtp-bar-tip-row-active');
            else
                r.actor.remove_style_class_name('wtp-bar-tip-row-active');
        }

        if (activeMeta?.kind === 'server') {
            this._total.text = `:${activeMeta.port} ${activeMeta.name}`;
            this._total.set_style(`color: ${activeMeta.color};`);
            this._totalCaption.text = `${activeMeta.memory} (${activeMeta.percent} of RAM)`;
            this._cpu.text = `CPU ${activeMeta.cpu}`;
            this._legend.text = activeMeta.subtitle
                ? `${activeMeta.subtitle}  ·  Click bar to expand`
                : `Other ${sum.other_memory}  ·  Free ${sum.free_memory}`;
            if (activeMeta.key)
                this._rows.get(activeMeta.key)?.add_style_class_name('wtp-server-item-highlight');
        } else if (activeMeta?.kind === 'other') {
            this._total.text = sum.other_memory;
            this._total.set_style(null);
            this._totalCaption.text = `other apps (${activeMeta.percent} of RAM)`;
            this._cpu.text = `CPU ${sum.cpu}`;
            this._legend.text = `Servers ${sum.servers_memory} (${count})  ·  Free ${sum.free_memory}`;
        } else if (activeMeta?.kind === 'free') {
            this._total.text = sum.free_memory;
            this._total.set_style(null);
            this._totalCaption.text = `free memory (${activeMeta.percent} of RAM)`;
            this._cpu.text = `CPU ${sum.cpu}`;
            this._legend.text = `Servers ${sum.servers_memory} (${count})  ·  Other ${sum.other_memory}`;
        } else {
            this._total.text = sum.servers_memory;
            this._total.set_style(null);
            this._totalCaption.text = `in ${count} server${count === 1 ? '' : 's'}`;
            this._cpu.text = `CPU ${sum.cpu}`;
            this._legend.text = `Other ${sum.other_memory}  ·  Free ${sum.free_memory}`;
        }
    }

    _applyServers(servers) {
        const keys = servers.map(s => s.key);
        const sameRows = keys.length === this._rows.size && keys.every(k => this._rows.has(k));
        if (!sameRows) {
            // moveMenuItem() leaves a submenu's body behind, so rebuild in order instead.
            const openKey = [...this._rows].find(([, row]) => row.menu.isOpen)?.[0];
            this._serverSection.removeAll();
            this._rows.clear();
            for (const s of servers) {
                const row = new ServerRow(this);
                this._rows.set(s.key, row);
                this._serverSection.addMenuItem(row);
            }
            this._rows.get(openKey)?.menu.open(false);
        }
        for (const s of servers)
            this._rows.get(s.key).update(s);
    }

    destroy() {
        if (this._signalId) {
            Gio.DBus.session.signal_unsubscribe(this._signalId);
            this._signalId = 0;
        }
        super.destroy();
    }
});

export default class WhatThePortExtension extends Extension {
    enable() {
        // The icon exists only while the wtp service is running; quitting removes it.
        this._onAppeared = () => this._show();
        this._onVanished = () => this._hide();
        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, BUS_NAME,
            Gio.BusNameWatcherFlags.NONE, this._onAppeared, this._onVanished);
    }

    disable() {
        if (this._watchId) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = 0;
        }
        this._hide();
    }

    _show() {
        if (this._indicator)
            return;
        this._indicator = new Indicator(this.path);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    _hide() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
