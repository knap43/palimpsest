# Palimpsest

A clipboard history for GNOME, built with GTK 4, libadwaita and Python.

It records everything you copy (text, images and files), keeps it on disk, and opens a searchable window on a key combination of your choosing.

## How it works

On Wayland, background apps cannot see the clipboard, so a small GNOME Shell extension (*Palimpsest Relay*) watches it from inside the shell. It forwards each copy to the app over D-Bus, and D-Bus activation starts the app on the first copy after login.

Entries that password managers mark as secret are never recorded.

Pasting works the same way in reverse: Wayland apps cannot type into other windows, so after copying, Palimpsest asks the extension to press Ctrl+V (Ctrl+Shift+V in terminals) once focus returns to the window you came from.

## Install

```sh
./install.sh
```

This needs `python-gobject`, `gtk4`, `libadwaita` and `gdk-pixbuf2`; the script tells you if any are missing.

Then:

1. Log out and back in, so GNOME Shell loads the extension.
2. In **Settings → Keyboard → Custom Shortcuts**, bind the command `~/.local/bin/palimpsest`, written out as a full path (for example `/home/you/.local/bin/palimpsest`).

## Use

| Key | Action |
| --- | --- |
| Your shortcut | Show or hide the window |
| Type | Search |
| Enter | Copy the selected entry and close; with **Paste After Copying** on, paste it too |
| ↑ / ↓ | Move through the list |
| Delete | Remove the selected entry |
| Esc | Clear the search, then close |
| Ctrl+Q | Quit |

**Paste After Copying** is a toggle in the menu, off by default and saved in `~/.config/palimpsest/settings.json`. After updating, log out and back in once so the shell picks up the new extension.

Pin an entry to keep it forever. Otherwise the 500 most recent entries are kept. **Clear Unpinned** in the menu empties the rest.

History lives in `~/.local/share/palimpsest/history.db`.
