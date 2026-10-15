// Palimpsest Relay — forwards every clipboard change to the Palimpsest app.
//
// On Wayland only the compositor sees the clipboard while an app is unfocused,
// so this extension watches it from inside GNOME Shell and calls the app's
// `push` action over D-Bus (D-Bus activation starts the app if needed).
// It also grants focus to the Palimpsest window when it is summoned by a
// shortcut, so you never get a "Palimpsest is ready" notification instead,
// and exports a `Paste` method: Wayland apps cannot synthesize keystrokes, so
// the app asks the shell to press Ctrl+V once focus returns to the previous
// window (Ctrl+Shift+V in terminals).

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const APP_ID = 'io.github.knap43.Palimpsest';
const APP_PATH = '/io/github/knap43/Palimpsest';
const MAX_BYTES = 32 * 1024 * 1024;

const RELAY_PATH = '/io/github/knap43/Palimpsest/Relay';
const RELAY_XML = `<node>
  <interface name="io.github.knap43.PalimpsestRelay">
    <method name="Paste"/>
  </interface>
</node>`;
const PASTE_POLL_MS = 40;         // how often to check whether focus has left Palimpsest
const PASTE_TIMEOUT_US = 1000000; // give up if it hasn't within a second

// Windows that paste with Ctrl+Shift+V, matched against WM class or app ID (lowercased).
const TERMINALS = new Set([
    'org.gnome.console', 'kgx', 'org.gnome.terminal', 'gnome-terminal-server',
    'org.gnome.ptyxis', 'ptyxis', 'com.raggesilver.blackbox', 'com.gexperts.tilix',
    'kitty', 'alacritty', 'foot', 'footclient', 'org.wezfurlong.wezterm',
    'com.mitchellh.ghostty', 'org.kde.konsole', 'konsole', 'xterm',
]);

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
            if (this._isOurs(win))
                Main.activateWindow(win);
        });

        // Pasting is optional: if any of it fails, capture and focus keep working.
        this._keyboard = null;
        this._pasteId = 0;
        try {
            this._dbus = Gio.DBusExportedObject.wrapJSObject(RELAY_XML, this);
            this._dbus.export(Gio.DBus.session, RELAY_PATH);
        } catch (e) {
            this._dbus = null;
            console.warn(`Palimpsest Relay: paste unavailable: ${e.message}`);
        }
    }

    disable() {
        this._cancelPaste();
        this._dbus?.unexport();
        this._dbus = null;
        this._keyboard?.run_dispose();
        this._keyboard = null;
        this._selection?.disconnect(this._ownerId);
        global.display.disconnect(this._attentionId);
        this._selection = null;
        this._clipboard = null;
    }

    _isOurs(win) {
        return win.get_gtk_application_id() === APP_ID || win.get_wm_class() === APP_ID;
    }

    // ── D-Bus: io.github.knap43.PalimpsestRelay ──

    Paste() {
        this._cancelPaste();
        const deadline = GLib.get_monotonic_time() + PASTE_TIMEOUT_US;
        this._pasteId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, PASTE_POLL_MS, () => {
            const win = global.display.focus_window;
            if (win && this._isOurs(win))
                return GLib.get_monotonic_time() < deadline ? GLib.SOURCE_CONTINUE : this._endPaste();
            this._endPaste();
            this._pressPaste(win);
            return GLib.SOURCE_REMOVE;
        });
    }

    _endPaste() {
        this._pasteId = 0;
        return GLib.SOURCE_REMOVE;
    }

    _cancelPaste() {
        if (this._pasteId)
            GLib.source_remove(this._pasteId);
        this._pasteId = 0;
    }

    _virtualKeyboard() {
        if (!this._keyboard) {
            const backend = global.stage.context?.get_backend?.() ?? Clutter.get_default_backend();
            this._keyboard = backend.get_default_seat()
                .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        }
        return this._keyboard;
    }

    _pressPaste(win) {
        try {
            this._sendPaste(win);
        } catch (e) {
            console.warn(`Palimpsest Relay: paste failed: ${e.message}`);
        }
    }

    _sendPaste(win) {
        const keyboard = this._virtualKeyboard();
        const ids = [win?.get_wm_class(), win?.get_gtk_application_id()];
        const terminal = ids.some(id => id && TERMINALS.has(id.toLowerCase()));
        const keys = terminal
            ? [Clutter.KEY_Control_L, Clutter.KEY_Shift_L, Clutter.KEY_v]
            : [Clutter.KEY_Control_L, Clutter.KEY_v];
        const time = GLib.get_monotonic_time();
        for (const key of keys)
            keyboard.notify_keyval(time, key, Clutter.KeyState.PRESSED);
        for (const key of keys.reverse())
            keyboard.notify_keyval(time, key, Clutter.KeyState.RELEASED);
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
