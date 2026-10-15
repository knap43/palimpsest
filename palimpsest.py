#!/usr/bin/env python3
"""Palimpsest — a clipboard history for GNOME.

Copies are captured by the companion GNOME Shell extension, which forwards each
one to this app's `push` action over D-Bus. Running `palimpsest` toggles the
window; bind that command to a key combination in GNOME Settings.

Enter copies the selected entry. With "Paste After Copying" on, the app then
asks the extension to paste it into the window that had focus before.
"""

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "io.github.knap43.Palimpsest"
DATA_DIR = Path(GLib.get_user_data_dir()) / "palimpsest"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "palimpsest" / "settings.json"
DEFAULTS = {"paste-after-copy": False}

SHELL_NAME = "org.gnome.Shell"  # the extension's object lives on the shell's connection
RELAY_PATH = "/io/github/knap43/Palimpsest/Relay"
RELAY_IFACE = "io.github.knap43.PalimpsestRelay"
MAX_ENTRIES = 500  # unpinned entries kept; pinned ones are never pruned
THUMB = 96

CSS = """
.thumb { border-radius: 6px; }
.leading { min-width: 48px; }
"""


# ── storage ──────────────────────────────────────────────────────────────────

class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS entries(
                id      INTEGER PRIMARY KEY,
                kind    TEXT NOT NULL,
                mime    TEXT NOT NULL,
                data    BLOB NOT NULL,
                text    TEXT NOT NULL,
                digest  TEXT NOT NULL UNIQUE,
                pinned  INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                used    REAL NOT NULL)"""
        )
        self.db.commit()

    def add(self, kind, mime, data, text):
        digest = hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()
        now = time.time()
        if self.db.execute("UPDATE entries SET used=? WHERE digest=?", (now, digest)).rowcount == 0:
            self.db.execute(
                "INSERT INTO entries(kind, mime, data, text, digest, created, used)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (kind, mime, data, text, digest, now, now),
            )
            self.db.execute(
                "DELETE FROM entries WHERE pinned=0 AND id NOT IN"
                " (SELECT id FROM entries WHERE pinned=0 ORDER BY used DESC LIMIT ?)",
                (MAX_ENTRIES,),
            )
        self.db.commit()

    def records(self):
        return self.db.execute(
            "SELECT id, kind, text, pinned, used FROM entries ORDER BY pinned DESC, used DESC"
        ).fetchall()

    def get(self, entry_id):
        return self.db.execute(
            "SELECT kind, mime, data FROM entries WHERE id=?", (entry_id,)
        ).fetchone()

    def _exec(self, sql, *args):
        self.db.execute(sql, args)
        self.db.commit()

    def touch(self, entry_id):
        self._exec("UPDATE entries SET used=? WHERE id=?", time.time(), entry_id)

    def set_pinned(self, entry_id, pinned):
        self._exec("UPDATE entries SET pinned=? WHERE id=?", int(pinned), entry_id)

    def delete(self, entry_id):
        self._exec("DELETE FROM entries WHERE id=?", entry_id)

    def clear_unpinned(self):
        self._exec("DELETE FROM entries WHERE pinned=0")


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.values = dict(DEFAULTS)
        try:
            stored = json.loads(path.read_text())
            self.values.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.values[key]

    def __setitem__(self, key, value):
        self.values[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, indent=2) + "\n")
        tmp.replace(self.path)


# ── clipboard payloads ───────────────────────────────────────────────────────

def parse_uris(mime, data):
    lines = data.decode("utf-8", "replace").replace("\0", "").splitlines()
    if mime == "x-special/gnome-copied-files" and lines and lines[0].strip() in ("copy", "cut"):
        lines = lines[1:]
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def uri_label(uri):
    return unquote(urlparse(uri).path) if uri.startswith("file://") else uri


def normalize(kind, mime, data):
    """Turn a raw capture into (kind, mime, data, searchable_text), or None."""
    if kind == "text":
        text = data.decode("utf-8", "replace").replace("\0", "").replace("\r\n", "\n")
        if not text.strip():
            return None
        return "text", "text/plain;charset=utf-8", text.encode(), text
    if kind == "files":
        uris = parse_uris(mime, data)
        if not uris:
            return None
        return "files", "text/uri-list", "\n".join(uris).encode(), "\n".join(map(uri_label, uris))
    if kind == "image":
        return "image", mime, data, ""
    return None


def files_provider(uris):
    def blob(mime, text):
        return Gdk.ContentProvider.new_for_bytes(mime, GLib.Bytes.new(text.encode()))

    return Gdk.ContentProvider.new_union([
        blob("x-special/gnome-copied-files", "copy\n" + "\n".join(uris)),
        blob("text/uri-list", "\r\n".join(uris) + "\r\n"),
        blob("text/plain;charset=utf-8", "\n".join(map(uri_label, uris))),
    ])


def thumbnail(data):
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
    pb = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, THUMB, THUMB, True, None)
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pb.get_width(), pb.get_height(), fmt,
                                 pb.read_pixel_bytes(), pb.get_rowstride())


def ago(ts):
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 7 * 86400:
        return f"{int(d // 86400)} d ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


# ── UI ───────────────────────────────────────────────────────────────────────

class EntryRow(Gtk.ListBoxRow):
    ICONS = {"text": "text-x-generic-symbolic", "files": "folder-documents-symbolic",
             "image": "image-x-generic-symbolic"}

    def __init__(self, window, record):
        super().__init__()
        self.entry_id, self.kind, text, pinned, used = record
        self.haystack = f"{text}\n{self.kind}".lower()

        if self.kind == "text":
            title, what = " ".join(text.split())[:400], "Text"
        elif self.kind == "files":
            paths = text.splitlines()
            title = ", ".join(Path(p).name or p for p in paths)
            what = "1 file" if len(paths) == 1 else f"{len(paths)} files"
        else:
            title, what = "Image", "Image"

        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=4, margin_end=4)
        box.append(window.leading_widget(self.entry_id, self.kind))

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                      valign=Gtk.Align.CENTER)
        col.append(Gtk.Label(label=title, xalign=0, wrap=True, lines=2,
                             wrap_mode=Pango.WrapMode.WORD_CHAR,
                             ellipsize=Pango.EllipsizeMode.END))
        col.append(Gtk.Label(label=f"{what} · {ago(used)}", xalign=0,
                             css_classes=["caption", "dim-label"]))
        box.append(col)

        pin = Gtk.ToggleButton(icon_name="view-pin-symbolic", active=bool(pinned),
                               valign=Gtk.Align.CENTER, can_focus=False,
                               tooltip_text="Pin", css_classes=["flat", "circular"])
        pin.connect("toggled", window.on_pin, self)
        box.append(pin)

        delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            can_focus=False, tooltip_text="Delete",
                            css_classes=["flat", "circular"])
        delete.connect("clicked", lambda *_: window.delete(self))
        box.append(delete)

        self.set_child(box)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Palimpsest",
                         default_width=560, default_height=640)
        self.app = app
        self.dirty = True
        self.words = []
        self.textures = {}
        self.set_hide_on_close(True)

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.search = Gtk.SearchEntry(placeholder_text="Search history", hexpand=True,
                                      search_delay=60)
        self.search.set_key_capture_widget(self)
        self.search.connect("search-changed", self._on_search_changed)
        self.search.connect("activate", self._on_search_activate)

        menu = Gio.Menu()
        options = Gio.Menu()
        options.append("Paste After Copying", "app.paste-after-copy")
        menu.append_section(None, options)
        actions = Gio.Menu()
        actions.append("Clear Unpinned", "win.clear")
        actions.append("Quit", "app.quit")
        menu.append_section(None, actions)
        header = Adw.HeaderBar(title_widget=self.search)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Menu"))

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                   css_classes=["navigation-sidebar"])
        self.listbox.set_filter_func(self._matches)
        self.listbox.connect("row-activated", self._on_row_activated)

        self.empty = Adw.StatusPage(icon_name="edit-paste-symbolic")
        self.stack = Gtk.Stack()
        self.stack.add_named(Gtk.ScrolledWindow(child=self.listbox, vexpand=True,
                                                hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.stack.add_named(self.empty, "empty")

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.set_content(view)

        clear = Gio.SimpleAction.new("clear", None)
        clear.connect("activate", self._on_clear)
        self.add_action(clear)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("notify::is-active", self._on_active_changed)

    # population

    def leading_widget(self, entry_id, kind):
        if kind == "image":
            tex = self._texture(entry_id)
            if tex is not None:
                return Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.COVER,
                                   width_request=48, height_request=48,
                                   overflow=Gtk.Overflow.HIDDEN, css_classes=["thumb"])
        return Gtk.Image(icon_name=EntryRow.ICONS[kind], pixel_size=24,
                         css_classes=["dim-label", "leading"])

    def _texture(self, entry_id):
        if entry_id not in self.textures:
            try:
                self.textures[entry_id] = thumbnail(self.app.history.get(entry_id)[2])
            except GLib.Error:
                self.textures[entry_id] = None
        return self.textures[entry_id]

    def refresh(self):
        records = self.app.history.records()
        live = {r[0] for r in records}
        self.textures = {k: v for k, v in self.textures.items() if k in live}
        self.listbox.remove_all()
        for rec in records:
            self.listbox.append(EntryRow(self, rec))
        self.dirty = False
        self._settle()

    def summon(self):
        if self.dirty:
            self.refresh()
        self.search.set_text("")
        self.words = []
        self.listbox.invalidate_filter()
        self._settle()
        self.present()
        self.search.grab_focus()

    # filtering and selection

    def _matches(self, row):
        return all(w in row.haystack for w in self.words)

    def _visible_rows(self):
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if self._matches(row):
                yield row
            i += 1

    def _settle(self):
        first = next(self._visible_rows(), None)
        if first:
            self.listbox.select_row(first)
            self.stack.set_visible_child_name("list")
        else:
            searching = bool(self.words)
            self.empty.set_title("No Matches" if searching else "Nothing Here Yet")
            self.empty.set_description(
                None if searching else "Text, images and files you copy will gather here.")
            self.stack.set_visible_child_name("empty")

    def _on_search_changed(self, entry):
        self.words = entry.get_text().lower().split()
        self.listbox.invalidate_filter()
        self._settle()

    def _on_search_activate(self, _entry):
        row = self.listbox.get_selected_row() or next(self._visible_rows(), None)
        if row:
            self._on_row_activated(self.listbox, row)

    # actions

    def _on_row_activated(self, _listbox, row):
        copied = self.app.restore(row.entry_id)
        self.set_visible(False)
        if copied and self.app.settings["paste-after-copy"]:
            self.app.paste()

    def on_pin(self, button, row):
        self.app.history.set_pinned(row.entry_id, button.get_active())
        GLib.idle_add(lambda: self.refresh() and False)

    def delete(self, row):
        rows = list(self._visible_rows())
        idx = rows.index(row) if row in rows else 0
        self.app.history.delete(row.entry_id)
        self.textures.pop(row.entry_id, None)
        self.listbox.remove(row)
        rows = list(self._visible_rows())
        if rows:
            nxt = rows[min(idx, len(rows) - 1)]
            self.listbox.select_row(nxt)
            if self.get_focus() is None or not self.get_focus().is_ancestor(self.search):
                nxt.grab_focus()
        else:
            self._settle()

    def _on_clear(self, *_):
        dialog = Adw.AlertDialog(heading="Clear History?",
                                 body="Every entry except pinned ones will be removed.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response):
            if response == "clear":
                self.app.history.clear_unpinned()
                self.refresh()

        dialog.connect("response", on_response)
        dialog.present(self)

    # keyboard and focus

    def _in_search(self):
        focus = self.get_focus()
        return focus is not None and (focus is self.search or focus.is_ancestor(self.search))

    def _on_key(self, _ctrl, keyval, _code, _state):
        if self.get_visible_dialog() is not None:
            return False
        if keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
                self.search.grab_focus()
            else:
                self.set_visible(False)
            return True

        rows = list(self._visible_rows())
        if self._in_search():
            if keyval in (Gdk.KEY_Down, Gdk.KEY_Page_Down) and rows:
                row = self.listbox.get_selected_row() or rows[0]
                self.listbox.select_row(row)
                row.grab_focus()
                return True
            return False

        row = self.listbox.get_selected_row()
        if keyval == Gdk.KEY_Up and rows and row is rows[0]:
            self.search.grab_focus()
            return True
        if keyval == Gdk.KEY_Delete and row is not None:
            self.delete(row)
            return True
        return False

    def _on_active_changed(self, *_):
        if not self.is_active() and self.get_visible() and self.get_visible_dialog() is None:
            self.set_visible(False)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.history = None
        self.settings = None
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()  # live on in the background to receive pushes
        self.history = History(DATA_DIR / "history.db")
        self.settings = Settings(CONFIG_FILE)

        paste = Gio.SimpleAction.new_stateful(
            "paste-after-copy", None, GLib.Variant.new_boolean(self.settings["paste-after-copy"]))
        paste.connect("change-state", self._on_paste_toggled)
        self.add_action(paste)

        push = Gio.SimpleAction.new("push", GLib.VariantType.new("(ssay)"))
        push.connect("activate", self._on_push)
        self.add_action(push)

        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        if self.window is None:
            self.window = Window(self)
        if self.window.get_visible() and self.window.is_active():
            self.window.set_visible(False)
        else:
            self.window.summon()

    def _on_paste_toggled(self, action, state):
        action.set_state(state)
        self.settings["paste-after-copy"] = state.get_boolean()

    def _on_push(self, _action, param):
        kind = param.get_child_value(0).unpack()
        mime = param.get_child_value(1).unpack()
        data = param.get_child_value(2).get_data_as_bytes().get_data()
        entry = normalize(kind, mime, data)
        if entry is None:
            return
        self.history.add(*entry)
        if self.window is not None:
            if self.window.get_visible():
                self.window.refresh()
            else:
                self.window.dirty = True

    def restore(self, entry_id):
        row = self.history.get(entry_id)
        if row is None:
            return False
        kind, _mime, data = row
        clipboard = Gdk.Display.get_default().get_clipboard()
        try:
            if kind == "text":
                clipboard.set_text(data.decode("utf-8", "replace"))
            elif kind == "image":
                clipboard.set_texture(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
            elif kind == "files":
                clipboard.set_content(files_provider(data.decode().split("\n")))
        except GLib.Error as err:
            print(f"palimpsest: could not restore entry {entry_id}: {err.message}",
                  file=sys.stderr)
            return False
        self.history.touch(entry_id)
        if self.window is not None:
            self.window.dirty = True
        return True

    def paste(self):
        """Ask the extension to paste into whichever window regains focus."""
        def done(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as err:
                print(f"palimpsest: could not paste (is the extension up to date?): "
                      f"{err.message}", file=sys.stderr)

        self.get_dbus_connection().call(
            SHELL_NAME, RELAY_PATH, RELAY_IFACE, "Paste", None, None,
            Gio.DBusCallFlags.NONE, 2000, None, done)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
