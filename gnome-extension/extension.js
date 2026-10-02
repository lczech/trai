import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';
import Cairo from 'cairo';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const DEFAULT_REFRESH = 180;
const WATCHDOG_SECONDS = 120; // trai itself bounds providers and locks; this is the last resort
const DIM = 140; // ~55% opacity, for "theme-dim" and dimmed text
const STALE = 128;
const TRACK_ALPHA = 0.25;
const LEVELS = ['ok', 'warn', 'critical'];
const FALLBACK_DISPLAY = {panel_box: 'right', panel_index: 0};
// Display-only menu rows: not clickable, but not greyed out like insensitive items.
const STATIC_ITEM = {activate: false, hover: false, can_focus: false};

function parseColor(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

function windowsOf(p) {
    return [p.short, p.long, ...p.windows].filter(w => w);
}

function formatReset(iso) {
    if (!iso)
        return '';
    const when = GLib.DateTime.new_from_iso8601(iso, null);
    if (!when)
        return '';
    const delta = when.to_unix() - GLib.get_real_time() / 1e6;
    if (delta <= 0)
        return 'reset due';
    if (delta < 86400) {
        const h = Math.floor(delta / 3600), m = Math.floor(delta % 3600 / 60);
        if (!h)
            return `resets in ${Math.max(m, 1)}m`;
        return m ? `resets in ${h}h ${m}m` : `resets in ${h}h`;
    }
    return `resets ${when.to_local().format('%a %H:%M')}`;
}

function formatAge(iso) {
    const when = iso && GLib.DateTime.new_from_iso8601(iso, null);
    if (!when)
        return 'never';
    const s = GLib.get_real_time() / 1e6 - when.to_unix();
    if (s < 60)
        return 'just now';
    if (s < 3600)
        return `${Math.floor(s / 60)} min ago`;
    if (s < 86400)
        return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
}

/** Apply a text colour spec ("theme", "theme-dim", "provider", "#rrggbb") to an actor. */
function applyColor(actor, spec, providerColor) {
    actor.style = null;
    actor.opacity = 255;
    if (spec === 'theme-dim')
        actor.opacity = DIM;
    else if (spec === 'provider')
        actor.style = `color: ${providerColor};`;
    else if (spec.startsWith('#'))
        actor.style = `color: ${spec};`;
}

const Pill = GObject.registerClass(
class TraiPill extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'trai', false);
        this._ext = ext;
        this._box = new St.BoxLayout({style_class: 'trai-pill', y_align: Clutter.ActorAlign.CENTER});
        this.add_child(this._box);

        this._ring = new St.DrawingArea({style_class: 'trai-ring', y_align: Clutter.ActorAlign.CENTER});
        this._ring.connect('repaint', area => this._drawRing(area));
        this._ringState = null;

        // A menu without items refuses to open, so it is also built on every
        // update; rebuilding on open refreshes the relative times.
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._buildMenu();
        });
    }

    /** Show a provider report; `gui` is the whole `trai status --gui` response. */
    showProvider(p, gui) {
        this._provider = p;
        this._message = null;
        this._gui = gui;
        const cfg = gui.config, d = cfg.display;
        const remaining = cfg.show === 'remaining';
        const rings = [p.short, p.long].filter(w => w); // text order: short first
        const dot = p.windows.map(w => w.level).reduce(
            (a, b) => LEVELS.indexOf(b) > LEVELS.indexOf(a) ? b : a, 'ok');

        const fill = w => w ? (remaining ? 100 - w.used_percent : w.used_percent) / 100 : 0;
        const [outer, inner] = d.outer_ring === 'short' ? [p.short, p.long] : [p.long, p.short];
        const single = rings.length === 1;
        this._setRing(p.color, fill(single ? rings[0] : outer), single ? null : fill(inner), d,
            dot === 'ok' ? null : d[`${dot}_color`]);

        const parts = [];
        if (d.show_name)
            parts.push(this._label(p.name, d.name_color, p.color));
        rings.forEach((w, i) => {
            if (i)
                parts.push(d.divider ? this._divider(d.label_color, p.color) : null);
            if (d.show_labels && w.label)
                parts.push(this._label(w.label, d.label_color, p.color));
            const value = Math.round(remaining ? 100 - w.used_percent : w.used_percent);
            const color = w.level === 'ok' ? d.value_color : d[`${w.level}_color`];
            parts.push(this._label(`${value}%`, color, p.color, 'trai-value'));
        });
        if (!rings.length)
            parts.push(this._label('–', d.value_color, p.color, 'trai-value'));
        if (p.stale || p.error || gui.config_error || gui.trai_error)
            parts.push(this._label('⚠', d.label_color, p.color));
        this._setParts(parts.filter(a => a));
        this._box.opacity = p.stale ? STALE : 255;
        this._buildMenu();
    }

    /** Show trai itself failing: a grey pill with a warning. */
    showMessage(message) {
        this._provider = null;
        this._message = message;
        this._setRing('#888888', 0, 0, {ring: {outer_diameter: 24, outer_width: 3, inner_diameter: 15, inner_width: 3}}, null);
        this._setParts([this._label('trai ⚠', 'theme', null)]);
        this._box.opacity = 255;
        this._buildMenu();
    }

    _label(text, colorSpec, providerColor, styleClass) {
        const label = new St.Label({text, y_align: Clutter.ActorAlign.CENTER, style_class: styleClass ?? ''});
        applyColor(label, colorSpec, providerColor);
        return label;
    }

    _divider(colorSpec, providerColor) {
        const divider = new St.DrawingArea({style_class: 'trai-divider', y_align: Clutter.ActorAlign.CENTER});
        applyColor(divider, colorSpec, providerColor);
        divider.connect('repaint', area => {
            const cr = area.get_context();
            const [w, h] = area.get_surface_size();
            const c = area.get_theme_node().get_foreground_color();
            cr.setSourceRGBA(c.red / 255, c.green / 255, c.blue / 255, c.alpha / 255);
            cr.rectangle(0, 0, w, h);
            cr.fill();
            cr.$dispose();
        });
        return divider;
    }

    _setParts(parts) {
        this._box.remove_all_children();
        this._box.add_child(this._ring);
        parts.forEach(a => this._box.add_child(a));
    }

    /** innerFraction null: a single ring, drawn with the outer ring's geometry. */
    _setRing(color, outerFraction, innerFraction, display, dot) {
        const r = display.ring;
        const size = Math.max(r.outer_diameter, r.inner_diameter);
        const ring = (diameter, width, frac) => ({radius: (diameter - width) / 2, width, frac});
        const rings = [ring(r.outer_diameter, r.outer_width, outerFraction)];
        if (innerFraction !== null)
            rings.push(ring(r.inner_diameter, r.inner_width, innerFraction));
        this._ringState = {size, color, rings, dot};
        this._ring.style = `width: ${size}px; height: ${size}px;`;
        this._ring.queue_repaint();
    }

    _drawRing(area) {
        const cr = area.get_context();
        const state = this._ringState;
        if (state) {
            const [w, h] = area.get_surface_size();
            cr.scale(Math.min(w, h) / state.size, Math.min(w, h) / state.size);
            const c = state.size / 2;
            const [r, g, b] = parseColor(state.color);
            cr.setLineCap(Cairo.LineCap.ROUND);
            for (const ring of state.rings) {
                cr.setLineWidth(ring.width);
                cr.setSourceRGBA(r, g, b, TRACK_ALPHA);
                cr.arc(c, c, ring.radius, 0, 2 * Math.PI);
                cr.stroke();
                const frac = Math.min(Math.max(ring.frac, 0), 1);
                if (frac > 0) {
                    cr.setSourceRGBA(r, g, b, 1);
                    cr.arc(c, c, ring.radius, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * frac);
                    cr.stroke();
                }
            }
            if (state.dot) {
                const [dr, dg, db] = parseColor(state.dot);
                cr.setSourceRGBA(dr, dg, db, 1);
                cr.arc(state.size - 2.5, 2.5, 2.5, 0, 2 * Math.PI);
                cr.fill();
            }
        }
        cr.$dispose();
    }

    _buildMenu() {
        this.menu.removeAll();
        if (this._message) {
            this._addText(this._message);
        } else if (this._provider) {
            const p = this._provider, gui = this._gui, d = gui.config.display;
            if (gui.trai_error)
                this._addText(`⚠ ${gui.trai_error}`);
            if (gui.config_error)
                this._addText(`⚠ Config: ${gui.config_error}`);
            if (p.error)
                this._addText(`⚠ Refresh failed (${p.error.kind}): ${p.error.message}`);
            const remaining = gui.config.show === 'remaining';
            for (const w of windowsOf(p)) {
                const value = Math.round(remaining ? 100 - w.used_percent : w.used_percent);
                const item = new PopupMenu.PopupBaseMenuItem({...STATIC_ITEM, style_class: 'trai-menu-row'});
                const label = new St.Label({text: w.label ?? w.id ?? '', style_class: 'trai-menu-label'});
                const val = new St.Label({text: `${value}% ${remaining ? 'left' : 'used'}`, style_class: 'trai-menu-value'});
                if (w.level !== 'ok')
                    val.style = `color: ${d[`${w.level}_color`]};`;
                const reset = new St.Label({text: formatReset(w.resets_at)});
                reset.opacity = DIM;
                [label, val, reset].forEach(a => item.add_child(a));
                this.menu.addMenuItem(item);
            }
            const info = [];
            if (p.extra.plan)
                info.push(`Plan: ${p.extra.plan}`);
            if (p.extra.cli_version)
                info.push(`CLI ${p.extra.cli_version}`);
            info.push(`Updated ${formatAge(p.fetched_at)}`);
            this._addText(info.join(' · '), true);
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Refresh now', () => this._ext.refresh(true));
    }

    _addText(text, dim = false) {
        const item = new PopupMenu.PopupMenuItem(text, STATIC_ITEM);
        item.label.add_style_class_name('trai-menu-text');
        item.label.clutter_text.line_wrap = true;
        item.label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        if (dim)
            item.label.opacity = DIM;
        this.menu.addMenuItem(item);
    }
});

export default class TraiExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._cancellable = new Gio.Cancellable();
        this._pills = new Map();
        this._levels = new Map();
        this._root = null;
        this._placement = null;
        this._timer = 0;
        this._watchdog = 0;
        this._lastGui = null;
        this._running = false;
        this._pending = false;
        this._refreshSeconds = DEFAULT_REFRESH;
        this.refresh(false);
    }

    disable() {
        this._cancellable.cancel();
        if (this._timer)
            GLib.source_remove(this._timer);
        if (this._watchdog)
            GLib.source_remove(this._watchdog);
        this._root?.destroy();
        this._source?.destroy();
        this._settings = this._cancellable = this._pills = this._levels = null;
        this._root = this._rootBox = this._source = this._placement = this._lastGui = null;
        this._timer = this._watchdog = 0;
    }

    _traiPath() {
        const configured = this._settings.get_string('trai-path');
        if (configured)
            return configured;
        const local = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'trai']);
        if (GLib.file_test(local, GLib.FileTest.IS_EXECUTABLE))
            return local;
        return GLib.find_program_in_path('trai');
    }

    refresh(force) {
        if (this._running) {
            this._pending ||= force;
            return;
        }
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        const trai = this._traiPath();
        if (!trai) {
            this._showFailure('trai CLI not found. Install it (see README), or set the trai-path setting.');
            this._schedule();
            return;
        }
        const argv = [trai, 'status', '--gui', ...force ? ['--refresh'] : []];
        let proc;
        try {
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            this._showFailure(`Could not run trai: ${e.message}`);
            this._schedule();
            return;
        }
        this._running = true;
        let timedOut = false;
        this._watchdog = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, WATCHDOG_SECONDS, () => {
            this._watchdog = 0;
            timedOut = true;
            proc.force_exit();
            return GLib.SOURCE_REMOVE;
        });
        const cancellable = this._cancellable;
        proc.communicate_utf8_async(null, cancellable, (_proc, result) => {
            if (cancellable.is_cancelled())
                return;
            this._running = false;
            if (this._watchdog) {
                GLib.source_remove(this._watchdog);
                this._watchdog = 0;
            }
            try {
                const [, stdout, stderr] = proc.communicate_utf8_finish(result);
                if (timedOut)
                    throw new Error(`no answer within ${WATCHDOG_SECONDS}s`);
                if (!proc.get_successful())
                    throw new Error(stderr.trim() || `trai exited with ${proc.get_exit_status()}`);
                const gui = JSON.parse(stdout);
                this._show(gui);
                this._lastGui = gui;
            } catch (e) {
                this._showFailure(`trai failed: ${e.message}`);
            }
            if (this._pending) {
                this._pending = false;
                this.refresh(true);
            } else {
                this._schedule();
            }
        });
    }

    _schedule() {
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, this._refreshSeconds, () => {
            this._timer = 0;
            this.refresh(false);
            return GLib.SOURCE_REMOVE;
        });
    }

    _ensureRoot(display) {
        const placement = `${display.panel_box}:${display.panel_index}`;
        if (this._root && placement === this._placement)
            return;
        this._root?.destroy();
        this._pills.clear();
        this._root = new PanelMenu.Button(0, 'trai', true);
        this._root.setSensitive(false);
        this._root.remove_style_class_name('panel-button');
        this._rootBox = new St.BoxLayout({style_class: 'trai-root'});
        this._root.add_child(this._rootBox);
        Main.panel.addToStatusArea(this.uuid, this._root, display.panel_index, display.panel_box);
        this._placement = placement;
    }

    /** Make the root hold exactly one pill per id, in this order. */
    _syncPills(ids) {
        for (const [id, pill] of this._pills) {
            if (!ids.includes(id)) {
                pill.destroy();
                this._pills.delete(id);
            }
        }
        ids.forEach((id, index) => {
            let pill = this._pills.get(id);
            if (!pill) {
                pill = new Pill(this);
                this._pills.set(id, pill);
                this._rootBox.add_child(pill.container);
                Main.panel.menuManager.addMenu(pill.menu);
            }
            if (this._rootBox.get_child_at_index(index) !== pill.container)
                this._rootBox.set_child_at_index(pill.container, index);
        });
        return ids.map(id => this._pills.get(id));
    }

    /** Keep the last data, dimmed with ⚠, if there is any; else a single message pill. */
    _showFailure(message) {
        const last = this._lastGui;
        if (!last?.providers.length) {
            this._showMessage(message);
            return;
        }
        this._show({
            ...last,
            trai_error: message,
            providers: last.providers.map(p => ({...p, stale: true})),
        }, false);
    }

    _show(gui, notify = true) {
        this._refreshSeconds = gui.config.refresh_seconds;
        this._ensureRoot(gui.config.display);
        if (!gui.providers.length) {
            const pill = this._syncPills(['trai'])[0];
            pill.showMessage(gui.config_error ? `Config: ${gui.config_error}` : 'No active providers.');
            return;
        }
        const pills = this._syncPills(gui.providers.map(p => p.id));
        gui.providers.forEach((p, i) => {
            pills[i].showProvider(p, gui);
            if (!notify)
                return;
            try {
                this._notifyLevels(p, gui.config.display);
            } catch (e) {
                console.error(`trai: notification failed: ${e}`);
            }
        });
    }

    _showMessage(message) {
        this._ensureRoot(this._placement ? {
            panel_box: this._placement.split(':')[0],
            panel_index: Number(this._placement.split(':')[1]),
        } : FALLBACK_DISPLAY);
        this._syncPills(['trai'])[0].showMessage(message);
    }

    _notifyLevels(p, display) {
        const keyed = [['short', p.short], ['long', p.long], ...p.windows.map(w => [w.id, w])];
        for (const [key, w] of keyed) {
            if (!w)
                continue;
            const id = `${p.id}/${key}`;
            const previous = this._levels.get(id) ?? 'ok';
            this._levels.set(id, w.level);
            if (LEVELS.indexOf(w.level) <= LEVELS.indexOf(previous) || !display[`notify_${w.level}`])
                continue;
            const used = Math.round(w.used_percent);
            this._sendNotification(`${p.name} ${w.label ?? key}: ${used}% used`, formatReset(w.resets_at),
                w.level === 'critical');
        }
    }

    _sendNotification(title, body, critical) {
        if (!this._source) {
            this._source = new MessageTray.Source({title: 'trai', iconName: 'dialog-warning-symbolic'});
            this._source.connect('destroy', () => {
                this._source = null;
            });
            Main.messageTray.add(this._source);
        }
        const notification = new MessageTray.Notification({
            source: this._source,
            title,
            body,
            urgency: critical ? MessageTray.Urgency.HIGH : MessageTray.Urgency.NORMAL,
        });
        this._source.addNotification(notification);
    }
}
