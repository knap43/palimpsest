// Palimpsest Relay — forwards every clipboard change to the Palimpsest app.
//
// On Wayland only the compositor sees the clipboard while an app is unfocused,
// so this extension watches it from inside GNOME Shell and calls the app's
// `push` action over D-Bus (D-Bus activation starts the app if needed).
// It also grants focus to the Palimpsest window when it is summoned by a
// shortcut, so you never get a "Palimpsest is ready" notification instead.

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const APP_ID = 'io.github.knap43.Palimpsest';
const APP_PATH = '/io/github/knap43/Palimpsest';
const MAX_BYTES = 32 * 1024 * 1024;

const SECRET = ['x-kde-passwordManagerHint'];  // password managers mark secrets with this
const TEXT = ['text/plain;charset=utf-8', 'UTF8_STRING', 'text/plain', 'STRING', 'TEXT'];
const IMAGES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp', 'image/tiff'];

export default class PalimpsestRelay extends Extension {
    enable() {
        this._clipboard = St.Clipboard.get_default();
        this._selection = global.display.get_selection();
        this._ownerId = this._selection.connect('owner-changed', (_sel, type) => {
            if (type === Meta.SelectionType.SELECTION_CLIPBOARD)
                this._capture();
        });
        this._attentionId = global.display.connect('window-demands-attention', (_d, win) => {
            if (win.get_gtk_application_id() === APP_ID || win.get_wm_class() === APP_ID)
                Main.activateWindow(win);
        });
    }

    disable() {
        this._selection?.disconnect(this._ownerId);
        global.display.disconnect(this._attentionId);
        this._selection = null;
        this._clipboard = null;
    }

    _capture() {
        const mimes = this._clipboard.get_mimetypes(St.ClipboardType.CLIPBOARD);
        if (!mimes.length || mimes.some(m => SECRET.includes(m)))
            return;

        const pick = list => list.find(m => mimes.includes(m));
        let kind, mime;
        if ((mime = pick(['x-special/gnome-copied-files'])))
            kind = 'files';
        else if ((mime = pick(TEXT)))
            kind = 'text';
        else if ((mime = pick(['text/uri-list'])))
            kind = 'files';
        else if ((mime = pick(IMAGES)))
            kind = 'image';
        else
            return;

        this._clipboard.get_content(St.ClipboardType.CLIPBOARD, mime, (_c, bytes) => {
            const size = bytes?.get_size() ?? 0;
            if (size > 0 && size <= MAX_BYTES)
                this._push(kind, mime, bytes);
        });
    }

    _push(kind, mime, bytes) {
        const payload = GLib.Variant.new_tuple([
            GLib.Variant.new_string(kind),
            GLib.Variant.new_string(mime),
            GLib.Variant.new_from_bytes(new GLib.VariantType('ay'), bytes, true),
        ]);
        const args = new GLib.Variant('(sava{sv})', ['push', [payload], {}]);

        Gio.DBus.session.call(APP_ID, APP_PATH, 'org.gtk.Actions', 'Activate',
            args, null, Gio.DBusCallFlags.NONE, -1, null, (conn, res) => {
                try {
                    conn.call_finish(res);
                } catch (e) {
                    console.warn(`Palimpsest Relay: ${e.message}`);
                }
            });
    }
}
