// Test-only helper for scripts/screenshots/run.sh: runs the JS in
// $TRAI_SHOTS_DIR/cmd.js and writes its (awaited) result to result.
// Never install this into a real session; it executes whatever is in that file.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const UUID = 'trai@lczech.github.io';
let pointer = null;

const root = () => Main.panel.statusArea[UUID];
const pills = () => root()?.get_first_child().get_children().map(c => c.child) ?? [];
const ext = () => Main.extensionManager.lookup(UUID)?.stateObj;

function shot(path, x, y, w, h) {
    // GNOME Shell crashes on an empty screenshot area, so clamp to the stage.
    x = Math.max(0, Math.round(x));
    y = Math.max(0, Math.round(y));
    w = Math.min(Math.round(w), global.stage.width - x);
    h = Math.min(Math.round(h), global.stage.height - y);
    if (w < 1 || h < 1)
        throw new Error(`empty screenshot area ${x},${y} ${w}x${h}`);
    const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
    return new Promise((resolve, reject) => {
        new Shell.Screenshot().screenshot_area(x, y, w, h, stream, (source, result) => {
            try {
                source.screenshot_area_finish(result);
                stream.close(null);
                resolve(path);
            } catch (e) {
                reject(e);
            }
        });
    });
}

function click(actor) {
    pointer ??= Clutter.get_default_backend().get_default_seat()
        .create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
    const [x, y] = actor.get_transformed_position();
    const [w, h] = actor.get_transformed_size();
    pointer.notify_absolute_motion(Clutter.CURRENT_TIME, x + w / 2, y + h / 2);
    pointer.notify_button(Clutter.CURRENT_TIME, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED);
    pointer.notify_button(Clutter.CURRENT_TIME, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED);
}

// Move the pointer off the panel, so no pill keeps a hover highlight.
function park() {
    pointer?.notify_absolute_motion(Clutter.CURRENT_TIME, global.stage.width / 2, global.stage.height - 1);
}

export default class ScreenshotHelper extends Extension {
    enable() {
        const dir = GLib.getenv('TRAI_SHOTS_DIR');
        if (!dir)
            return; // only ever active inside scripts/screenshots/run.sh
        const t = {Main, root, pills, ext, shot, click, park};
        let busy = false;
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 200, () => {
            const cmd = `${dir}/cmd.js`;
            if (busy || !GLib.file_test(cmd, GLib.FileTest.EXISTS))
                return GLib.SOURCE_CONTINUE;
            busy = true;
            const [, bytes] = GLib.file_get_contents(cmd);
            GLib.unlink(cmd);
            const write = text => {
                GLib.file_set_contents(`${dir}/result`, String(text));
                busy = false;
            };
            try {
                const body = new TextDecoder().decode(bytes);
                Promise.resolve(new Function('t', body)(t)).then(write, e => write(`ERROR ${e}`));
            } catch (e) {
                write(`ERROR ${e}`);
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        pointer = null;
    }
}
